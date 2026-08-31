# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from local_first_agent_os.coordination import DispatchKind
from local_first_agent_os.coordination.dispatch import submit_dispatch_intent
from local_first_agent_os.coordination.store import now, tx
from local_first_agent_os.dispatcher_runner import _context_for_intent
from local_first_agent_os.interrupted_recovery import (
    InterruptedEffectsConflict,
    RetainedWorktree,
    inspect_interrupted_attempt,
    recovery_for_intent,
)
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject


def _failed_payload(worktrees: Sequence[Mapping[str, object]]) -> str:
    return json.dumps(
        {
            "schema_version": "dispatch_runner_result.v1",
            "run_result": {
                "status": "FAILED",
                "tasks": [
                    {
                        "task_name": f"task-{index}",
                        "artifacts": [
                            {
                                "artifact_type": "cli_agent_run",
                                "content": {
                                    "streaming_supervisor": {"preserve_worktree": True},
                                    "worktree": worktree,
                                    "changed_files": ["src/changed.py"],
                                },
                            }
                        ],
                    }
                    for index, worktree in enumerate(worktrees)
                ],
            },
        }
    )


def _record_interrupted_intent(
    work_unit_id: str,
    milestone_key: str,
    payload: str,
) -> str:
    submitted = submit_dispatch_intent(
        "senior",
        "implement",
        kind="code",
        target_project_id="local_first_agent_os",
        source=f"work_unit:{work_unit_id}:milestone_execution:{milestone_key}",
    )
    intent_id = str(submitted["intent_id"])
    with tx() as c:
        c.execute(
            "UPDATE dispatch_intents SET status='FAILED', outcome='TRANSPORT_INTERRUPTED', "
            "result=?, error='transport interrupted', completed_at=? WHERE intent_id=?",
            (payload, now(), intent_id),
        )
    return intent_id


def test_retry_reuses_the_exact_retained_worktree(
    work_unit_ledger: Path,
    tmp_path: Path,
) -> None:
    retained = tmp_path / "retained"
    retained.mkdir()
    worktree = {
        "worktree_path": str(retained),
        "source_repo_path": str(tmp_path / "source"),
        "head_sha": "a" * 40,
    }
    previous = _record_interrupted_intent("wu-1", "implement", _failed_payload([worktree]))

    inspected = inspect_interrupted_attempt("wu-1", "implement", 2)

    assert isinstance(inspected, RetainedWorktree)
    assert inspected.previous_intent_id == previous
    assert inspected.worktree_path == str(retained)
    assert inspected.changed_files == ("src/changed.py",)


def test_conflicting_retained_effects_refuse_a_successor(
    work_unit_ledger: Path,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _record_interrupted_intent(
        "wu-2",
        "implement",
        _failed_payload(
            [
                {
                    "worktree_path": str(first),
                    "source_repo_path": str(tmp_path / "source"),
                    "head_sha": "a" * 40,
                },
                {
                    "worktree_path": str(second),
                    "source_repo_path": str(tmp_path / "source"),
                    "head_sha": "a" * 40,
                },
            ]
        ),
    )

    inspected = inspect_interrupted_attempt("wu-2", "implement", 2)

    assert isinstance(inspected, InterruptedEffectsConflict)
    assert "conflicting worktrees" in inspected.reason


def test_recovery_effect_is_transactionally_bound_to_the_successor_intent(
    work_unit_ledger: Path,
    tmp_path: Path,
) -> None:
    retained = tmp_path / "retained"
    retained.mkdir()
    recovery = RetainedWorktree(
        effect_id="ie-test",
        previous_intent_id="intent-previous",
        worktree_path=str(retained),
        source_repo_path=str(tmp_path / "source"),
        base_head_sha="a" * 40,
        changed_files=("src/changed.py",),
        evidence_hash="evidence",
    )

    submitted = submit_dispatch_intent(
        "senior",
        "implement",
        kind="code",
        target_project_id="local_first_agent_os",
        interrupted_recovery=recovery.to_payload(),
    )

    assert recovery_for_intent(str(submitted["intent_id"])) == recovery


def test_dispatch_context_uses_recovery_worktree_instead_of_allocating_another(
    work_unit_ledger: Path,
    tmp_path: Path,
) -> None:
    retained = tmp_path / "retained"
    retained.mkdir()
    recovery = RetainedWorktree(
        effect_id="ie-context",
        previous_intent_id="intent-previous",
        worktree_path=str(retained),
        source_repo_path=str(tmp_path / "source"),
        base_head_sha="b" * 40,
        changed_files=("src/changed.py",),
        evidence_hash="evidence",
    )
    submitted = submit_dispatch_intent(
        "senior",
        "implement",
        kind="code",
        target_project_id="local_first_agent_os",
        interrupted_recovery=recovery.to_payload(),
    )
    target = LinkedProject(
        id="local_first_agent_os",
        kind="python",
        path=tmp_path / "source",
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="test target",
        verification_commands=[],
    )
    intent = {"intent_id": submitted["intent_id"], "source": "work_unit:wu-1"}

    context = _context_for_intent(
        saga_id="saga-1",
        prompt="implement",
        intent=intent,
        target_project=target,
        kind=DispatchKind.CODE,
    )

    assert context.reuse_checkpoint_worktree is True
    assert context.checkpoint_worktree_path == str(retained)
    assert context.checkpoint_base_head_sha == "b" * 40
