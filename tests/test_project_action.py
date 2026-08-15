# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute
from refinery_support import build_target_repository, code_merge_payload, git, write_registry_config

import local_first_agent_os.api as api_module
import local_first_agent_os.project_action as project_action_module
from local_first_agent_os.contracts import SourceType, WorkflowStatus, WorkspaceId
from local_first_agent_os.coordination import store
from local_first_agent_os.coordination.approvals import (
    resolve_approval_request,
    submit_approval_request,
)
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.directives import DirectiveParser
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.merge_review import pending_code_merge_approval
from local_first_agent_os.project_action import (
    ExecutionKind,
    IntentFacts,
    LeaseFacts,
    ProjectActionKind,
    ProjectActionSnapshot,
    build_project_action_snapshot,
)
from local_first_agent_os.settings import Settings, get_settings
from local_first_agent_os.workflow.engine import WorkflowEngine


class FakeSource:
    def __init__(self, facts: dict[str, Any]):
        self.facts = facts

    def read_project_action_facts(self, project_id: str) -> dict[str, Any]:
        assert project_id == "pest_site_factory"
        return self.facts


def base_facts() -> dict[str, Any]:
    saga_id = "saga-product"
    milestone_id = f"{saga_id}:m06_hosted_preview"
    return {
        "project": {
            "id": "pest_site_factory",
            "path": "/tmp/pest_site_factory",
            "exists": True,
            "git_repo": True,
            "branch": "main",
            "head_sha": "abc123",
        },
        "sagas": [
            {
                "saga_id": saga_id,
                "gawd_doc_id": "gawd-1",
                "status": "EXECUTING",
                "updated_at": "2026-07-17T12:00:00+00:00",
            }
        ],
        "milestones": [
            {
                "milestone_id": f"{saga_id}:m05_verification",
                "name": "Verification",
                "sequence": 5,
                "status": "COMPLETED",
            },
            {
                "milestone_id": milestone_id,
                "name": "Hosted preview",
                "sequence": 6,
                "status": "PENDING",
            },
        ],
        "intents": [
            {
                "intent_id": "intent-m5",
                "target_project_id": "pest_site_factory",
                "source": f"approved_gawd:gawd-1:milestone:{saga_id}:m05_verification",
                "status": "DONE",
                "created_at": "2026-07-17T11:00:00+00:00",
            }
        ],
        "leases": [],
        "checkpoints": [
            {
                "checkpoint_id": "old-checkpoint",
                "intent_id": "intent-m5",
                "saga_id": saga_id,
                "source_repo_path": "/tmp/pest_site_factory",
                "status": "PAUSED",
                "created_at": "2026-07-17T10:00:00+00:00",
            }
        ],
        "approvals": [],
    }


def snapshot(facts: dict[str, Any]):
    settings = Settings(mock_models=True)
    return build_project_action_snapshot(
        "pest_site_factory",
        settings=settings,
        source=FakeSource(facts),
        generated_at=datetime(2026, 7, 17, 13, 0, tzinfo=UTC),
    )


def test_next_hosted_preview_milestone_requires_deploy_approval() -> None:
    result = snapshot(base_facts())

    assert result.schema_version == "project_action_snapshot.v1"
    assert result.action is ProjectActionKind.DEPLOY_APPROVAL_REQUIRED
    assert result.milestone and result.milestone.name == "Hosted preview"
    assert result.next_command == (
        "pi /start /approved-gawd gawd-1 --target-project pest_site_factory"
    )
    # A checkpoint for a completed older milestone must not mask the current action.
    assert result.checkpoint is None


def test_active_lease_is_working() -> None:
    facts = base_facts()
    milestone_id = facts["milestones"][1]["milestone_id"]
    facts["intents"].append(
        {
            "intent_id": "intent-m6",
            "target_project_id": "pest_site_factory",
            "source": f"approved_gawd:gawd-1:milestone:{milestone_id}",
            "status": "CLAIMED",
            "created_at": "2026-07-17T12:10:00+00:00",
        }
    )
    facts["leases"] = [
        {
            "lease_id": "lease-m6",
            "intent_id": "intent-m6",
            "status": "ACTIVE",
            "activity_status": "RUNNING_COMMAND",
            "created_at": "2026-07-17T12:11:00+00:00",
        }
    ]

    result = snapshot(facts)

    assert result.action is ProjectActionKind.WORKING
    # A lease supersedes its intent, and the snapshot says which shape it is
    # rather than leaving the reader to probe for a lease_id.
    assert isinstance(result.execution, LeaseFacts)
    assert result.execution.execution_kind is ExecutionKind.LEASE
    assert result.execution.lease_id == "lease-m6"
    assert result.execution.activity_status == "RUNNING_COMMAND"


def test_current_checkpoint_is_recoverable_failure() -> None:
    facts = base_facts()
    milestone_id = facts["milestones"][1]["milestone_id"]
    facts["intents"].append(
        {
            "intent_id": "intent-m6",
            "target_project_id": "pest_site_factory",
            "source": f"approved_gawd:gawd-1:milestone:{milestone_id}",
            "status": "FAILED",
            "created_at": "2026-07-17T12:10:00+00:00",
        }
    )
    facts["checkpoints"].append(
        {
            "checkpoint_id": "checkpoint-m6",
            "intent_id": "intent-m6",
            "saga_id": "saga-product",
            "source_repo_path": "/tmp/pest_site_factory",
            "status": "PAUSED",
            "created_at": "2026-07-17T12:12:00+00:00",
        }
    )

    result = snapshot(facts)

    assert result.action is ProjectActionKind.RECOVERABLE_FAILURE
    assert result.checkpoint and result.checkpoint.checkpoint_id == "checkpoint-m6"


def test_git_owner_failure_blocks_instead_of_guessing() -> None:
    facts = base_facts()
    facts["project"]["git_error"] = "git rev-parse HEAD failed"

    result = snapshot(facts)

    assert result.action is ProjectActionKind.BLOCKED
    assert result.next_command is None
    assert result.warnings == ["git rev-parse HEAD failed"]


def test_project_status_directive_requires_one_project(runtime: Any) -> None:
    parser = DirectiveParser(runtime.settings)

    spec = parser.parse("/project-status pest_site_factory")

    assert spec.action == "project_status"
    assert spec.target_project_id == "pest_site_factory"
    with pytest.raises(ValueError, match="exactly one"):
        parser.parse("/project-status")


def test_project_status_directive_returns_snapshot_artifact(
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = snapshot(base_facts())
    monkeypatch.setattr(
        project_action_module,
        "build_project_action_snapshot",
        lambda project_id, settings: expected,
    )
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/project-status pest_site_factory"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    assert result.status is WorkflowStatus.COMPLETED
    artifact = next(item for item in result.artifacts if str(item.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert payload["action"] == "project_status"
    assert payload["snapshot"]["schema_version"] == "project_action_snapshot.v1"


def test_project_action_http_endpoint(
    runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = snapshot(base_facts())
    monkeypatch.setattr(api_module, "get_settings", lambda: runtime.settings)
    monkeypatch.setattr(api_module, "get_runtime", lambda: runtime)
    monkeypatch.setattr(
        api_module,
        "build_project_action_snapshot",
        lambda project_id, settings: expected,
    )

    app = api_module.create_app()
    route = cast(
        APIRoute,
        next(
            route
            for route in app.routes
            if getattr(route, "path", None) == "/projects/{project_id}/action"
        ),
    )
    response = route.endpoint("pest_site_factory")

    assert "GET" in route.methods
    # The route returns the model and declares it. The declaration is what puts a
    # real shape in the published schema, which is what the generated TypeScript
    # client is built from; a route that quietly went back to returning a dict
    # would publish a bare object and take the client's typing with it.
    assert route.response_model is ProjectActionSnapshot
    assert response.action == "DEPLOY_APPROVAL_REQUIRED"


def test_an_intent_without_a_lease_is_its_own_narrower_shape() -> None:
    """Before a lease exists the intent is the whole truth, and it says so.

    The previous flattened shape reported agent, supervisor, and persistence lanes
    for work that had not started, because one bag of optional fields cannot
    distinguish "no agent yet" from "an agent reporting nothing".
    """

    facts = base_facts()
    milestone_id = facts["milestones"][1]["milestone_id"]
    facts["intents"].append(
        {
            "intent_id": "intent-m6",
            "target_project_id": "pest_site_factory",
            "source": f"approved_gawd:gawd-1:milestone:{milestone_id}",
            "status": "PENDING",
            "tier": "senior",
            "kind": "code",
            "created_at": "2026-07-17T12:10:00+00:00",
        }
    )

    result = snapshot(facts)

    assert isinstance(result.execution, IntentFacts)
    assert result.execution.execution_kind is ExecutionKind.INTENT
    assert result.execution.intent_id == "intent-m6"
    assert result.execution.tier == "senior"
    assert not hasattr(result.execution, "supervisor_status")


def test_an_unnamed_ledger_column_does_not_reach_the_published_contract() -> None:
    """The projection is a named list of columns, not a pass-through of the row.

    Adding a column to `agent_execution_leases` used to add a public API field by
    accident, which is the Hyrum's law hazard of forwarding raw rows. Now a column
    reaches the cockpit when someone adds it to the model.
    """

    facts = base_facts()
    milestone_id = facts["milestones"][1]["milestone_id"]
    facts["intents"].append(
        {
            "intent_id": "intent-m6",
            "target_project_id": "pest_site_factory",
            "source": f"approved_gawd:gawd-1:milestone:{milestone_id}",
            "status": "CLAIMED",
            "created_at": "2026-07-17T12:10:00+00:00",
        }
    )
    facts["leases"] = [
        {
            "lease_id": "lease-m6",
            "intent_id": "intent-m6",
            "status": "ACTIVE",
            "created_at": "2026-07-17T12:11:00+00:00",
            "some_new_internal_column": "must not be published",
        }
    ]

    result = snapshot(facts)

    published = result.model_dump(mode="json")
    assert "some_new_internal_column" not in (published["execution"] or {})
    assert isinstance(result.execution, LeaseFacts)
    assert result.execution.lease_id == "lease-m6"


def test_an_unrecognized_lease_status_blocks_instead_of_falling_through() -> None:
    """A status this runtime does not know must not produce a confident action.

    Before the statuses were parsed into their enums, an unknown value matched no
    literal set and fell through the whole chain to the default branch, so the
    cockpit told the operator to approve a GAWD milestone on the strength of a
    value nobody understood.
    """

    facts = base_facts()
    milestone_id = facts["milestones"][1]["milestone_id"]
    facts["intents"].append(
        {
            "intent_id": "intent-m6",
            "target_project_id": "pest_site_factory",
            "source": f"approved_gawd:gawd-1:milestone:{milestone_id}",
            "status": "CLAIMED",
            "created_at": "2026-07-17T12:10:00+00:00",
        }
    )
    facts["leases"] = [
        {
            "lease_id": "lease-m6",
            "intent_id": "intent-m6",
            "status": "QUANTUM_SUPERPOSITION",
            "created_at": "2026-07-17T12:11:00+00:00",
        }
    ]

    result = snapshot(facts)

    assert result.action is ProjectActionKind.BLOCKED
    assert result.next_command is None
    assert any("QUANTUM_SUPERPOSITION" in warning for warning in result.warnings)
    # The warning names the vocabulary it checked against, so an operator can see
    # whether the ledger or the runtime is the stale side.
    assert any("known values are" in warning for warning in result.warnings)


def test_a_missing_status_is_also_a_refusal() -> None:
    """An absent status is the same hazard as an unknown one, by a different route."""

    facts = base_facts()
    milestone_id = facts["milestones"][1]["milestone_id"]
    facts["intents"].append(
        {
            "intent_id": "intent-m6",
            "target_project_id": "pest_site_factory",
            "source": f"approved_gawd:gawd-1:milestone:{milestone_id}",
            "created_at": "2026-07-17T12:10:00+00:00",
        }
    )

    result = snapshot(facts)

    assert result.action is ProjectActionKind.BLOCKED
    assert any("carries no status" in warning for warning in result.warnings)


def test_a_milestone_with_an_unreadable_status_is_not_treated_as_finished() -> None:
    """Selection reads an unparseable status conservatively: still in play.

    Treating it as settled would hide the milestone from the operator entirely,
    which is worse than blocking on it.
    """

    facts = base_facts()
    for item in facts["milestones"]:
        item["status"] = "COMPLETED"
    facts["milestones"][1]["status"] = "NOT_A_STATUS"

    result = snapshot(facts)

    assert result.action is ProjectActionKind.BLOCKED
    assert result.action is not ProjectActionKind.COMPLETE
    assert any("NOT_A_STATUS" in warning for warning in result.warnings)


def test_known_statuses_still_drive_the_action_they_always_did() -> None:
    """The enum rewrite is a change of representation, not of behavior."""

    facts = base_facts()
    milestone_id = facts["milestones"][1]["milestone_id"]
    facts["intents"].append(
        {
            "intent_id": "intent-m6",
            "target_project_id": "pest_site_factory",
            "source": f"approved_gawd:gawd-1:milestone:{milestone_id}",
            "status": "PENDING",
            "created_at": "2026-07-17T12:10:00+00:00",
        }
    )

    result = snapshot(facts)

    assert result.action is ProjectActionKind.WORKING
    assert result.next_command == "pi /dispatch"
    assert result.warnings == []


# ---------------------------------------------------------------------------
# The merge the operator is told to perform
# ---------------------------------------------------------------------------


def _approved_merge_facts(
    *, repository_path: str, commit_sha: str, head_sha: str
) -> dict[str, Any]:
    """A saga whose current milestone has an approved CODE_MERGE against it."""

    facts = base_facts()
    saga_id = facts["sagas"][0]["saga_id"]
    facts["project"]["path"] = repository_path
    facts["project"]["head_sha"] = head_sha
    # The stale PAUSED checkpoint belongs to an older milestone and would
    # otherwise win the chain before the approval is ever considered.
    facts["checkpoints"] = []
    facts["approvals"] = [
        {
            "approval_id": "approval-merge",
            "saga_id": saga_id,
            "request_type": "CODE_MERGE",
            "status": "APPROVED",
            "created_at": "2026-07-17T12:30:00+00:00",
            "payload": {
                "target_project_id": "pest_site_factory",
                "milestone_id": f"{saga_id}:m06_hosted_preview",
                "commit_sha": commit_sha,
            },
        }
    ]
    return facts


def test_the_merge_command_the_cockpit_names_actually_lands_the_commit(tmp_path: Path) -> None:
    """The regression, stated as the property rather than as the string.

    The cockpit used to name `pi /approve-merge <id>` here. That directive
    resolves a PENDING approval and the approval in this state is already
    APPROVED, so the one action the operator was offered could only ever raise.
    Asserting a different literal would not have caught that, because the old
    literal was a perfectly well formed command; what it was not was runnable.

    So this runs it. The repository is real, the command comes out of the
    snapshot rather than out of this test, and the assertion is that HEAD moved
    to the approved commit.

    The shape is checked before anything is executed, and that guard is not
    ceremony. Running whatever string the cockpit produced is how the first
    draft of this test spent thirty seconds invoking the developer's real `pi`
    binary against their real ledger, because the unfixed code names a `pi`
    command and there is one on the PATH. A test that executes its subject's
    output has to bound what it is willing to execute first.
    """

    repository = build_target_repository(tmp_path / "target")
    facts = _approved_merge_facts(
        repository_path=str(repository.path),
        commit_sha=repository.commit_sha,
        head_sha=repository.base_sha,
    )

    result = snapshot(facts)

    assert result.action is ProjectActionKind.MERGE_INTEGRATION_REQUIRED
    assert result.next_command
    argv = shlex.split(result.next_command)
    assert argv[:3] == ["git", "-C", str(repository.path)], (
        "the cockpit must name a git command scoped to this project's repository; "
        f"got {result.next_command!r}"
    )

    completed = subprocess.run(argv, capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    assert git(repository.path, "rev-parse", "HEAD") == repository.commit_sha


def test_the_approve_directive_is_not_runnable_once_the_approval_is_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why the old command was wrong, pinned against the directive it named.

    Without this, someone reading the change sees only that a string was swapped
    and can swap it back. This drives the real ledger: an approved CODE_MERGE
    exists, and the lookup `/approve-merge` performs refuses to find it. Any
    future `next_command` beginning with that directive is refuted here rather
    than in front of an operator.
    """

    repository = build_target_repository(tmp_path / "target")
    write_registry_config(tmp_path / "configs", repository.path, project_id="target")
    monkeypatch.setenv("LOCAL_AGENT_CONFIG_DIR", str(tmp_path / "configs"))
    get_settings.cache_clear()
    store.set_root(str(tmp_path / "coordination"))

    saga = create_saga("Land an approved agent branch")
    approval = submit_approval_request(
        str(saga["saga_id"]),
        "CODE_MERGE",
        payload=code_merge_payload(repository, project_id="target"),
        requested_by="dispatcher_runner",
    )
    approval_id = str(approval["approval_id"])
    resolved = resolve_approval_request(approval_id, approved=True, resolved_by="operator")
    assert resolved["ok"] is True

    settings = Settings(mock_models=True)
    with pytest.raises(ValueError, match="No pending CODE_MERGE approval"):
        pending_code_merge_approval(settings=settings, approval_id=approval_id)


def test_a_landed_merge_still_names_something_the_operator_can_run(tmp_path: Path) -> None:
    """The state one step after the fix must not be a dead end.

    Once the fast-forward above succeeds, the approved commit is HEAD and the
    milestone is still open. The merge branch used to capture that state, match
    none of its own cases, and return the chain's initial summary with
    `next_command = None` - so following the cockpit's instruction left the
    operator with nothing. It now falls through to the approved-GAWD path, which
    is the code that detects a contained approved commit and prints the exact
    `complete_saga_milestone` call.
    """

    repository = build_target_repository(tmp_path / "target")
    facts = _approved_merge_facts(
        repository_path=str(repository.path),
        commit_sha=repository.commit_sha,
        head_sha=repository.commit_sha,
    )

    result = snapshot(facts)

    assert result.action is not ProjectActionKind.MERGE_INTEGRATION_REQUIRED
    assert result.next_command == (
        "pi /start /approved-gawd gawd-1 --target-project pest_site_factory"
    )


def test_an_approved_merge_naming_no_commit_blocks_rather_than_going_quiet(
    tmp_path: Path,
) -> None:
    """Fail closed, which is what the module claims to be.

    Enqueue refuses to queue a CODE_MERGE whose payload names no commit, so no
    approval submitted since can reach this. A row that predates that refusal
    still has to get an answer, and "an operator decision is required" with no
    command is not one.
    """

    repository = build_target_repository(tmp_path / "target")
    facts = _approved_merge_facts(
        repository_path=str(repository.path),
        commit_sha=repository.commit_sha,
        head_sha=repository.base_sha,
    )
    facts["approvals"][0]["payload"].pop("commit_sha")

    result = snapshot(facts)

    assert result.action is ProjectActionKind.BLOCKED
    assert result.next_command is None
    assert any("approval-merge" in warning for warning in result.warnings)
