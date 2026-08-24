# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from local_first_agent_os.contracts import SourceType, WorkflowStatus, WorkspaceId
from local_first_agent_os.coordination.durable import (
    CoordinationCommandRequest,
    ExternalAgentLeaseBoundaryRequest,
    open_external_agent_execution_lease_durably,
    run_coordination_command_durably,
)
from local_first_agent_os.coordination.store import tx
from local_first_agent_os.dispatcher import Dispatched, Idle, LedgerDispatcher
from local_first_agent_os.dispatcher_runner import DispatcherIntentRunner
from local_first_agent_os.engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.pow_wow import (
    PowWowArtifact,
    PowWowRunResult,
    PowWowTaskResult,
    resolve_coordination_events_path,
    run_coordination_command,
)
from local_first_agent_os.pow_wow.protocol import PlanningPhase
from local_first_agent_os.tools_gate import ToolGate, shelly_plug
from local_first_agent_os.workflow import WorkflowEngine


def _coord(root: Path, args: list[str]) -> dict:
    return run_coordination_command(args, root=root)


def _run_git_command(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _run_git_command(["git", "init"], path)
    _run_git_command(["git", "config", "user.email", "test@example.com"], path)
    _run_git_command(["git", "config", "user.name", "Test User"], path)
    (path / "README.md").write_text("# target\n", encoding="utf-8")
    _run_git_command(["git", "add", "README.md"], path)
    _run_git_command(["git", "commit", "-m", "initial"], path)


def _write_linked_projects(
    config_dir: Path,
    target_path: Path,
    *,
    verification_commands: tuple[str, ...] = ("true",),
) -> None:
    verification = ", ".join(json.dumps(command) for command in verification_commands)
    (config_dir / "linked_projects.toml").write_text(
        f"""
[center]
id = "local_first_agent_os"
description = "test center"
control_plane_project = "target"
default_saga_project = "target"
default_memory_project = "target"

[[projects]]
id = "target"
kind = "test_repo"
path = {json.dumps(str(target_path))}
status = "active"
read_only = false
description = "test target"
primary_interfaces = ["pytest"]
owns = ["tests"]
avoid = []
verification_commands = [{verification}]
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_dispatch_intent_lifecycle_and_atomic_claim(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    a = _coord(
        root, ["submit_dispatch_intent", "junior", "OCR this file", "--source", "gemma_scan"]
    )
    b = _coord(root, ["submit_dispatch_intent", "staff", "review marketability"])
    assert a["status"] == "PENDING" and b["tier"] == "staff"

    pending = _coord(root, ["list_dispatch_intents", "--status", "PENDING"])["intents"]
    assert len(pending) == 2

    # oldest-first, atomic claim: two claims never return the same intent
    first = _coord(root, ["claim_next_dispatch_intent", "--claimed-by", "d1"])["intent"]
    second = _coord(root, ["claim_next_dispatch_intent", "--claimed-by", "d2"])["intent"]
    assert first["intent_id"] == a["intent_id"]  # FIFO
    assert second["intent_id"] == b["intent_id"]
    assert first["intent_id"] != second["intent_id"]

    # a tier-scoped claim only takes that tier
    _coord(root, ["submit_dispatch_intent", "junior", "another junior task"])
    staff_claim = _coord(
        root, ["claim_next_dispatch_intent", "--claimed-by", "d3", "--tier", "staff"]
    )
    assert staff_claim["intent"] is None  # no PENDING staff intents left

    _coord(root, ["complete_dispatch_intent", first["intent_id"], "DONE", "--result", "text"])
    done = _coord(root, ["list_dispatch_intents", "--status", "DONE"])["intents"]
    assert done[0]["intent_id"] == first["intent_id"]
    assert done[0]["result"] == "text"


def test_large_dispatch_result_uses_file_transport(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    intent = _coord(root, ["submit_dispatch_intent", "junior", "produce a large result"])
    _coord(root, ["claim_next_dispatch_intent", "--claimed-by", "worker"])
    result = "r" * (70 * 1024)

    _coord(
        root,
        ["complete_dispatch_intent", intent["intent_id"], "DONE", "--result", result],
    )
    done = _coord(root, ["list_dispatch_intents", "--status", "DONE"])["intents"]

    assert done[0]["result"] == result
    assert not list((root / ".agent_coordination").glob("coordination-payload-*.txt"))


def test_large_approval_payload_uses_file_transport(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    saga = _coord(root, ["create_saga", "Approve a large patch"])
    payload = {"changed_files": [f"generated/site-{index}.html" for index in range(6_000)]}

    request = _coord(
        root,
        [
            "submit_approval_request",
            saga["saga_id"],
            "CODE_MERGE",
            "--payload",
            json.dumps(payload),
        ],
    )
    pending = _coord(root, ["list_approval_requests", "--saga-id", saga["saga_id"]])

    assert pending["requests"][0]["approval_id"] == request["approval_id"]
    assert pending["requests"][0]["payload"] == payload
    assert not list((root / ".agent_coordination").glob("coordination-payload-*.txt"))


def test_ledger_event_outbox_is_disabled_without_a_destination(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    _coord(
        root,
        ["submit_dispatch_intent", "junior", "OCR this file", "--source", "gemma_scan"],
    )

    assert _coord(root, ["list_ledger_events"])["events"] == []


def test_ledger_event_outbox_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "LOCAL_AGENT_LEDGER_OUTBOX",
        '{"mode":"configured","consumer":"reactor","topic":"coordination"}',
    )
    root = tmp_path / "coord"
    intent = _coord(
        root,
        ["submit_dispatch_intent", "junior", "OCR this file", "--source", "gemma_scan"],
    )

    pending = _coord(root, ["list_ledger_events", "--status", "PENDING"])["events"]
    assert len(pending) == 1
    assert pending[0]["event_type"] == "submit_dispatch_intent"
    assert pending[0]["aggregate_type"] == "dispatch_intent"
    assert pending[0]["aggregate_id"] == intent["intent_id"]
    assert pending[0]["payload"]["intent_id"] == intent["intent_id"]

    claimed = _coord(
        root,
        ["claim_next_ledger_event", "--claimed-by", "reactor"],
    )["event"]
    assert claimed["event_id"] == pending[0]["event_id"]
    assert claimed["status"] == "CLAIMED"
    assert claimed["attempts"] == 1

    _coord(root, ["complete_ledger_event", claimed["event_id"], "PROCESSED"])
    processed = _coord(root, ["list_ledger_events", "--status", "PROCESSED"])["events"]
    assert [event["event_id"] for event in processed] == [claimed["event_id"]]
    assert _coord(root, ["claim_next_ledger_event", "--claimed-by", "reactor"])["event"] is None


def test_high_volume_internal_events_stay_out_of_the_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heartbeats and step events reach events.jsonl but never the outbox table.

    They are the bulk of everything emitted and no consumer projects them, so
    mirroring them would grow ledger_events without bound.
    """

    monkeypatch.setenv(
        "LOCAL_AGENT_LEDGER_OUTBOX",
        '{"mode":"configured","consumer":"reactor","topic":"coordination"}',
    )
    root = tmp_path / "coord"
    intent = _coord(root, ["submit_dispatch_intent", "senior", "noisy target"])
    lease = _coord(
        root,
        [
            "open_execution_lease",
            "intent:noise:senior",
            "--worker-id",
            "worker-1",
            "--intent-id",
            intent["intent_id"],
            "--timeout-seconds",
            "60",
        ],
    )["lease"]

    _coord(root, ["heartbeat_execution_lease", lease["lease_id"], "--worker-id", "worker-1"])
    payload = json.dumps({"text": "hello"}, sort_keys=True, separators=(",", ":"))
    _coord(
        root,
        [
            "append_execution_event",
            lease["lease_id"],
            "--sequence",
            "1",
            "--occurred-at",
            "1.0",
            "--source",
            "stdout",
            "--kind",
            "output",
            "--payload",
            payload,
            "--payload-sha256",
            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        ],
    )
    _coord(root, ["gc"])

    mirrored = {event["event_type"] for event in _coord(root, ["list_ledger_events"])["events"]}
    assert mirrored == {"submit_dispatch_intent", "open_execution_lease"}

    logged = [
        json.loads(line)["event_type"]
        for line in resolve_coordination_events_path(root=root)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert "heartbeat_execution_lease" in logged
    assert "append_execution_event" in logged
    assert "gc_ledger" in logged


def test_execution_lease_lifecycle_and_idempotency(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    intent = _coord(root, ["submit_dispatch_intent", "senior", "lease target"])
    opened = _coord(
        root,
        [
            "open_execution_lease",
            "intent:test:senior",
            "--worker-id",
            "worker-1",
            "--intent-id",
            intent["intent_id"],
            "--agent-tier",
            "senior",
            "--agent-name",
            "claude",
            "--worktree-path",
            str(tmp_path / "worktree"),
            "--command-json",
            '["claude","--print"]',
            "--compensation-json",
            '{"cleanup":"remove_worktree"}',
            "--timeout-seconds",
            "60",
        ],
    )
    lease = opened["lease"]
    assert opened["created"] is True
    assert lease["status"] == "ACTIVE"
    assert lease["command"] == ["claude", "--print"]
    assert lease["compensation"] == {"cleanup": "remove_worktree"}

    repeated = _coord(
        root,
        [
            "open_execution_lease",
            "intent:test:senior",
            "--worker-id",
            "worker-1",
            "--timeout-seconds",
            "60",
        ],
    )
    assert repeated["created"] is False
    assert repeated["lease"]["lease_id"] == lease["lease_id"]

    heartbeat = _coord(
        root,
        ["heartbeat_execution_lease", lease["lease_id"], "--worker-id", "worker-1"],
    )
    assert heartbeat["cancel_requested"] is False

    cancel = _coord(
        root,
        [
            "request_execution_cancel",
            lease["lease_id"],
            "--reason",
            "operator changed target",
            "--requested-by",
            "test",
        ],
    )
    assert cancel["lease"]["status"] == "CANCEL_REQUESTED"

    heartbeat = _coord(
        root,
        ["heartbeat_execution_lease", lease["lease_id"], "--worker-id", "worker-1"],
    )
    assert heartbeat["cancel_requested"] is True

    completed = _coord(
        root,
        [
            "complete_execution_lease",
            lease["lease_id"],
            "CANCELED",
            "--result-json",
            '{"stopped":true}',
        ],
    )
    assert completed["lease"]["status"] == "CANCELED"
    assert completed["lease"]["result"] == {"stopped": True}
    canceled = _coord(root, ["list_execution_leases", "--status", "CANCELED"])["leases"]
    assert [row["lease_id"] for row in canceled] == [lease["lease_id"]]


def test_a_root_scoped_command_uses_the_inherited_schema(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """`--root` says which directory, not which database engine.

    It used to mean both: passing a root forced SQLite, which is how 694
    subprocess ledger calls got their isolation. Isolation is a schema now, so the
    child inherits the parent's and writes where the parent can read it.
    """

    root = tmp_path / "coord"
    schema = os.environ["AGENT_COORDINATION_SCHEMA"]

    _coord(root, ["submit_dispatch_intent", "junior", "local smoke task"])

    # Written by a subprocess, read by this process: same schema, same rows.
    with tx() as connection:
        intents = [
            dict(row)
            for row in connection.execute(
                "SELECT prompt FROM dispatch_intents ORDER BY created_at"
            ).fetchall()
        ]
        current = dict(connection.execute("SELECT current_schema() AS name").fetchone())

    assert current["name"] == schema
    assert [item["prompt"] for item in intents] == ["local smoke task"]


def test_coordination_dbos_boundary_direct_path(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    result = run_coordination_command_durably(
        CoordinationCommandRequest(
            args=["submit_dispatch_intent", "junior", "durable command smoke"],
            coordination_root=str(root),
        )
    )

    assert result["status"] == "PENDING"


def test_external_agent_lease_dbos_boundary_direct_path(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    result = open_external_agent_execution_lease_durably(
        ExternalAgentLeaseBoundaryRequest(
            idempotency_key="intent:test:staff",
            worker_id="staff-worker",
            agent_tier="staff",
            agent_name="codex",
            worktree_path=str(tmp_path / "worktree"),
            command=["codex", "exec"],
            compensation={"cleanup": "remove_worktree"},
            coordination_root=str(root),
        )
    )

    assert result["ok"] is True
    lease = result["lease_open_result"]["lease"]
    assert lease["status"] == "ACTIVE"
    assert lease["command"] == ["codex", "exec"]
    assert lease["timeout_seconds"] == 3600


def test_generated_worktree_files_do_not_trigger_dispatch_without_ledger_intent(
    tmp_path: Path,
    runtime,
) -> None:
    root = tmp_path / "coord"
    runtime.settings.coordination_root = root
    generated = (
        tmp_path / "generated-worktree" / "src" / "local_first_agent_os" / "generated_workflows"
    )
    generated.mkdir(parents=True)
    (generated / "bestanswers_bot.py").write_text(
        "# generated workflow stub\n",
        encoding="utf-8",
    )

    def fail_if_called(_intent):
        raise AssertionError("dispatcher runner should not run without a ledger intent")

    dispatcher = LedgerDispatcher(
        fail_if_called,
        name="generated-file-proof",
        settings=runtime.settings,
    )

    assert _coord(root, ["list_dispatch_intents"])["intents"] == []
    assert isinstance(dispatcher.poll_once(), Idle)
    assert _coord(root, ["list_dispatch_intents"])["intents"] == []


def test_dispatch_intent_cancel_and_supersede(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    canceled = _coord(root, ["submit_dispatch_intent", "senior", "wrong target"])
    cancel = _coord(
        root,
        [
            "cancel_dispatch_intent",
            canceled["intent_id"],
            "--reason",
            "target missing",
            "--canceled-by",
            "test",
        ],
    )
    assert cancel["status"] == "CANCELED"

    claim = _coord(root, ["claim_next_dispatch_intent", "--claimed-by", "d1"])
    assert claim["intent"] is None
    canceled_rows = _coord(root, ["list_dispatch_intents", "--status", "CANCELED"])["intents"]
    assert canceled_rows[0]["intent_id"] == canceled["intent_id"]
    assert canceled_rows[0]["error"] == "target missing"

    old = _coord(
        root,
        [
            "submit_dispatch_intent",
            "senior",
            "old prompt",
            "--kind",
            "code",
            "--target-project-id",
            "wrong",
            "--source",
            "approved_gawd:test",
        ],
    )
    superseded = _coord(
        root,
        [
            "supersede_dispatch_intent",
            old["intent_id"],
            "--target-project-id",
            "right",
            "--reason",
            "route to explicit target",
        ],
    )
    assert superseded["old_intent_id"] == old["intent_id"]
    assert superseded["new_intent_id"] != old["intent_id"]

    pending = _coord(root, ["list_dispatch_intents", "--status", "PENDING"])["intents"]
    assert len(pending) == 1
    assert pending[0]["intent_id"] == superseded["new_intent_id"]
    assert pending[0]["target_project_id"] == "right"


def test_reactor_poll_runs_and_records(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    _coord(root, ["submit_dispatch_intent", "junior", "do the thing"])
    seen = []

    def runner(intent):
        seen.append(intent["prompt"])
        return ("DONE", f"ran: {intent['prompt']}", None)

    from local_first_agent_os.settings import Settings

    settings = Settings(coordination_root=root)
    dispatcher = LedgerDispatcher(runner, name="r1", settings=settings)

    outcome = dispatcher.poll_once()
    assert isinstance(outcome, Dispatched) and outcome.status == "DONE"
    assert seen == ["do the thing"]
    # queue drained -> next poll is Idle
    assert isinstance(dispatcher.poll_once(), Idle)
    done = _coord(root, ["list_dispatch_intents", "--status", "DONE"])["intents"]
    assert done[0]["result"] == "ran: do the thing"


def test_reactor_runner_crash_fails_intent_not_reactor(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    _coord(root, ["submit_dispatch_intent", "senior", "boom"])
    from local_first_agent_os.settings import Settings

    def runner(intent):
        raise RuntimeError("kaboom")

    dispatcher = LedgerDispatcher(runner, name="r2", settings=Settings(coordination_root=root))
    outcome = dispatcher.poll_once()
    assert isinstance(outcome, Dispatched) and outcome.status == "FAILED"
    failed = _coord(root, ["list_dispatch_intents", "--status", "FAILED"])["intents"]
    assert "kaboom" in failed[0]["error"]


def test_dispatcher_runner_executes_intent_through_pow_wow_ledger(tmp_path: Path, runtime) -> None:
    root = tmp_path / "coord"
    target = tmp_path / "target"
    target.mkdir()
    runtime.settings.coordination_root = root
    runtime.settings.saga_worktree_root = tmp_path / "worktrees"
    _write_linked_projects(runtime.settings.config_dir, target)

    class FakeExecutor:
        def __init__(self) -> None:
            self.calls = []

        def dispatch_pow_wow(self, pow_wow_id, target_project, tasks, context):
            self.calls.append(
                {
                    "pow_wow_id": pow_wow_id,
                    "target_project_id": target_project.id,
                    "tasks": tasks,
                    "context": context,
                }
            )
            task_results = []
            for task in tasks:
                artifacts = [
                    PowWowArtifact(
                        artifact_type="fake_dispatch_output",
                        schema_version="fake_dispatch_output.v1",
                        task_name=task.task_name,
                        content={"ok": True, "dispatch_kind": task.dispatch_kind},
                    )
                ]
                if task.planning_phase is PlanningPhase.SENIOR_OWNED_PLAN:
                    artifacts.append(
                        PowWowArtifact(
                            artifact_type="worktree_commit_checkpoint",
                            schema_version="worktree_commit_checkpoint.v1",
                            task_name=task.task_name,
                            content={
                                "branch_name": "agent/fake-reviewed",
                                "base_head_sha": "a" * 40,
                                "commit_sha": "b" * 40,
                                "commit_created": True,
                                "changed_from_base": True,
                                "checkpointed_files": ["changed.txt"],
                            },
                        )
                    )
                if task.planning_phase is PlanningPhase.STAFF_FINAL_REVIEW:
                    artifacts.append(
                        PowWowArtifact(
                            artifact_type="review_result",
                            schema_version="review_result.v1",
                            task_name=task.task_name,
                            content={
                                "schema_version": "review_result.v1",
                                "verdict": "approve",
                                "review_origin": "AUTOMATED_STAFF",
                                "reviewer_tier": "STAFF",
                                "harness": "codex",
                                "model": "gpt-5.6-sol",
                                "execution_lease_id": "lease-fake-review",
                                "task_id": context.task_ids_by_name[task.task_name],
                                "reviewed_commit_sha": "b" * 40,
                                "base_sha": "a" * 40,
                                "completion_status": "COMPLETED",
                                "engineering_doctrine": (
                                    CURRENT_ENGINEERING_DOCTRINE.provenance_payload()
                                ),
                                "provenance_stamped_by": "pow_wow_executor",
                            },
                        )
                    )
                task_results.append(
                    PowWowTaskResult(
                        task_name=task.task_name,
                        role=task.role,
                        status="completed",
                        summary=f"fake executor completed {task.task_name}",
                        changed_files=("changed.txt",)
                        if task.planning_phase is PlanningPhase.SENIOR_OWNED_PLAN
                        else (),
                        artifacts=tuple(artifacts),
                    )
                )
            return PowWowRunResult(
                executor="FakeExecutor",
                mode="cli",
                pow_wow_id=pow_wow_id,
                target_project_id=target_project.id,
                target_project_path=str(target_project.expanded_path),
                status="COMPLETED",
                output_summary="fake runner ok",
                tasks=tuple(task_results),
                changed_files=("changed.txt",),
                external_agents_started=True,
                auto_merge=False,
            )

    fake = FakeExecutor()
    runner = DispatcherIntentRunner(runtime, executor_factory=lambda _bench, _ceiling: fake)  # type: ignore[arg-type]
    _coord(
        root,
        [
            "submit_dispatch_intent",
            "senior",
            "make a safe change",
            "--kind",
            "code",
            "--target-project-id",
            "target",
        ],
    )
    dispatcher = LedgerDispatcher(runner, name="test-dispatcher", settings=runtime.settings)

    outcome = dispatcher.poll_once()

    assert isinstance(outcome, Dispatched)
    assert outcome.status == "DONE"
    assert len(fake.calls) == 1
    assert fake.calls[0]["context"].dispatch_kind == "code"
    planned_tasks = fake.calls[0]["tasks"]
    assert [task.judgment.tier.value for task in planned_tasks] == [
        "senior",
        "staff",
        "junior",
        "senior",
        "staff",
    ]
    assert planned_tasks[2].blocked_by == (planned_tasks[0].task_name,)
    assert set(planned_tasks[3].blocked_by) == {
        planned_tasks[0].task_name,
        planned_tasks[2].task_name,
    }
    assert set(planned_tasks[4].blocked_by) == {
        planned_tasks[1].task_name,
        planned_tasks[3].task_name,
    }
    done = _coord(root, ["list_dispatch_intents", "--status", "DONE"])["intents"]
    payload = json.loads(done[0]["result"])
    assert payload["schema_version"] == "dispatch_runner_result.v1"
    assert payload["result_origin"] == "AUTOMATED"
    assert payload["result_state"] == "COMPLETED"
    assert payload["promotion_state"] == "MERGE_PENDING"
    assert payload["pow_wow_id"] == fake.calls[0]["pow_wow_id"]
    assert payload["decomposition"]["schema_version"] == "decomposition_plan.v1"
    assert payload["decomposition"]["mini_gawd"]["schema_version"] == "mini_gawd_doc.v1"
    assert payload["decomposition"]["mini_gawd"]["scope"]["non_goals"][0].startswith(
        "No automatic merge"
    )
    assert len(payload["task_ids_by_name"]) == 5
    assert payload["merge_approval"]["status"] == "PENDING"
    pending_approvals = _coord(root, ["list_approval_requests", "--status", "PENDING"])
    review_packet = pending_approvals["requests"][0]["payload"]["review_packet"]
    assert review_packet["schema_version"] == "merge_review_packet.v1"
    assert review_packet["approval_is_separate"] is True
    assert review_packet["changed_files"] == ["changed.txt"]
    pow_wow = _coord(root, ["get_pow_wow", payload["pow_wow_id"]])["pow_wow"]
    assert pow_wow["status"] == "COMPLETED"
    tasks = _coord(root, ["list_tasks", payload["pow_wow_id"]])["tasks"]
    tasks_by_name = {task["task_name"]: task for task in tasks}
    assert all(task["status"] == "COMPLETED" for task in tasks)
    assert tasks_by_name[planned_tasks[0].task_name]["blocked_by"] == []
    assert tasks_by_name[planned_tasks[1].task_name]["blocked_by"] == []
    assert tasks_by_name[planned_tasks[2].task_name]["blocked_by"] == [planned_tasks[0].task_name]
    assert tasks_by_name[planned_tasks[3].task_name]["blocked_by"] == [
        planned_tasks[0].task_name,
        planned_tasks[2].task_name,
    ]
    assert tasks_by_name[planned_tasks[4].task_name]["blocked_by"] == [
        planned_tasks[1].task_name,
        planned_tasks[3].task_name,
    ]


def test_dispatcher_runner_rejects_code_intent_without_target(
    tmp_path: Path,
    runtime,
) -> None:
    root = tmp_path / "coord"
    target = tmp_path / "target"
    target.mkdir()
    runtime.settings.coordination_root = root
    runtime.settings.saga_worktree_root = tmp_path / "worktrees"
    _write_linked_projects(runtime.settings.config_dir, target)
    runner = DispatcherIntentRunner(runtime)
    submitted = _coord(
        root,
        [
            "submit_dispatch_intent",
            "senior",
            "old pre-routing code intent",
            "--kind",
            "code",
        ],
    )
    dispatcher = LedgerDispatcher(runner, name="test-dispatcher", settings=runtime.settings)

    outcome = dispatcher.poll_once()

    assert isinstance(outcome, Dispatched)
    assert outcome.status == "FAILED"
    failed = _coord(root, ["list_dispatch_intents", "--status", "FAILED"])["intents"]
    assert failed[0]["intent_id"] == submitted["intent_id"]
    assert "requires target_project_id" in failed[0]["error"]
    with tx() as conn:
        saga_count = dict(conn.execute("SELECT COUNT(*) AS n FROM sagas").fetchone())["n"]
        pow_wow_count = dict(conn.execute("SELECT COUNT(*) AS n FROM pow_wows").fetchone())["n"]
    assert saga_count == 0
    assert pow_wow_count == 0


def test_approved_gawd_dispatcher_runs_fake_frontier_clis_end_to_end(
    tmp_path: Path,
    runtime,
) -> None:
    root = tmp_path / "coordination-root"
    target = tmp_path / "target"
    runtime.settings.coordination_root = root
    runtime.settings.saga_worktree_root = tmp_path / "worktrees"
    _init_git_repo(target)
    _write_linked_projects(
        runtime.settings.config_dir,
        target,
        verification_commands=("test -f NEXT_STEP.md",),
    )
    (runtime.settings.config_dir / "staffing.toml").write_text(
        """
seated_pairing = "two-vendor"

[pairings.two-vendor.staff]
harness = "codex"
model = "gpt-5.6-sol"
reasoning_effort = "high"
capacity = 1

[pairings.two-vendor.senior]
harness = "claude"
capacity = 3

[bench.junior]
harness = "pi"
model = "gemma4"
capacity = 4
""".strip()
        + "\n",
        encoding="utf-8",
    )
    claude = tmp_path / "fake_claude.py"
    claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "prompt = sys.argv[-1]\n"
        "if 'Planning visibility contract: senior_independent_reading.' in prompt:\n"
        "    print(json.dumps({'type':'result','result':'independent raw-contract reading'}))\n"
        "    raise SystemExit(0)\n"
        "Path('NEXT_STEP.md').write_text('proof from fake claude\\n', encoding='utf-8')\n"
        "print(json.dumps({'type':'result','result':'created NEXT_STEP.md'}))\n",
        encoding="utf-8",
    )
    claude.chmod(0o755)
    codex = tmp_path / "fake_codex.py"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:3] == ['login', 'status']:\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.argv[-1]\n"
        "if 'Planning visibility contract: staff_independent_reading.' in prompt:\n"
        "    print('Independent staff raw-contract reading')\n"
        "    raise SystemExit(0)\n"
        "if not Path('NEXT_STEP.md').exists():\n"
        "    print('VERDICT: BLOCK - missing NEXT_STEP.md')\n"
        "    raise SystemExit(2)\n"
        "print('VERDICT: APPROVE - NEXT_STEP.md present for dispatcher boundary proof')\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    saga = run_coordination_command(
        ["create_saga", "Dispatcher boundary proof"],
        settings=runtime.settings,
    )
    doc = run_coordination_command(
        [
            "create_gawd_doc",
            "Dispatcher boundary proof",
            "--saga-id",
            saga["saga_id"],
            "--constraints",
            "Only touch the disposable proof target.",
            "--success-criteria",
            "NEXT_STEP.md is created in the isolated worktree.",
            "--acceptance-criteria",
            "Dispatcher claims and completes the intent.",
            "--task-graph-json",
            '{"schema_version":"new_project_task_graph.v1"}',
        ],
        settings=runtime.settings,
    )
    directive = f"/start /approved-gawd {doc['gawd_doc_id']} --target-project target"
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": directive},
    )

    approval_result = WorkflowEngine(runtime).model_directive(event)

    assert approval_result.status == WorkflowStatus.COMPLETED
    approval_artifact = next(
        artifact
        for artifact in approval_result.artifacts
        if str(artifact.role) == "directive_result"
    )
    approval_payload = runtime.artifact_store.read_json(approval_artifact.artifact_id)
    assert approval_payload["status"] == "approved_and_enqueued"
    assert approval_payload["target_project_id"] == "target"

    delegate_calls: list[dict] = []

    def fake_delegate(**kwargs):
        delegate_calls.append(kwargs)
        return {"ok": True, "output": f"junior context for {kwargs['task_name']}", "metadata": {}}

    runner = DispatcherIntentRunner(
        runtime,
        delegate_fn=fake_delegate,
        claude_bin=str(claude),
        codex_bin=str(codex),
    )
    dispatcher = LedgerDispatcher(runner, name="proof-dispatcher", settings=runtime.settings)

    outcome = dispatcher.poll_once()

    assert isinstance(outcome, Dispatched)
    assert outcome.status == "DONE"
    assert not (target / "NEXT_STEP.md").exists()
    assert delegate_calls and delegate_calls[0]["model"] == "gemma4"
    done = _coord(root, ["list_dispatch_intents", "--status", "DONE"])["intents"]
    assert done[0]["intent_id"] == approval_payload["dispatch_intent_id"]
    payload = json.loads(done[0]["result"])
    assert payload["schema_version"] == "dispatch_runner_result.v1"
    assert payload["target_project_id"] == "target"
    assert payload["merge_approval"]["status"] == "PENDING"
    milestones = _coord(root, ["list_saga_milestones", approval_payload["saga_id"]])["milestones"]
    assert milestones[0]["status"] == "COMPLETED"
    evidence = _coord(root, ["get_saga_milestone", milestones[0]["milestone_id"]])["milestone"][
        "evidence"
    ]
    assert evidence and evidence[0]["evidence_type"] == "summary"
    run_result = payload["run_result"]
    assert run_result["status"] == "COMPLETED"
    assert run_result["external_agents_started"] is True
    assert run_result["auto_merge"] is False
    assert run_result["changed_files"] == ["NEXT_STEP.md"]
    tasks_by_role = {task["role"]: task for task in run_result["tasks"]}
    assert tasks_by_role["independent_reader"]["status"] == "completed"
    assert tasks_by_role["independent_reviewer"]["status"] == "completed"
    assert tasks_by_role["verification_planner"]["status"] == "completed"
    assert tasks_by_role["implementer"]["changed_files"] == ["NEXT_STEP.md"]
    review_run_capture = next(
        artifact["content"]
        for artifact in tasks_by_role["reviewer"]["artifacts"]
        if artifact["artifact_type"] == "cli_agent_run"
    )
    assert "APPROVE" in review_run_capture["verdict"]


def test_recovery_staff_review_reuses_exact_commit_and_stamps_host_provenance(
    tmp_path: Path,
    runtime,
) -> None:
    root = tmp_path / "coordination-root"
    target = tmp_path / "target"
    runtime.settings.coordination_root = root
    runtime.settings.saga_worktree_root = tmp_path / "worktrees"
    _init_git_repo(target)
    _write_linked_projects(runtime.settings.config_dir, target)
    (runtime.settings.config_dir / "staffing.toml").write_text(
        """
seated_pairing = "two-vendor"

[pairings.two-vendor.staff]
harness = "codex"
model = "gpt-5.6-sol"
reasoning_effort = "high"
capacity = 1

[pairings.two-vendor.senior]
harness = "claude"
capacity = 3

[bench.junior]
harness = "pi"
model = "gemma4"
capacity = 4
""".strip()
        + "\n",
        encoding="utf-8",
    )
    original_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    retained_branch = "agent/retained-implementation"
    _run_git_command(["git", "checkout", "-b", retained_branch], target)
    (target / "FEATURE.md").write_text("retained implementation\n", encoding="utf-8")
    _run_git_command(["git", "add", "FEATURE.md"], target)
    _run_git_command(["git", "commit", "-m", "Retain implementation for staff review"], target)
    retained_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _run_git_command(["git", "checkout", original_branch], target)

    saga = _coord(root, ["create_saga", "Recover retained implementation"])
    original = _coord(
        root,
        [
            "submit_dispatch_intent",
            "senior",
            "Implement the retained change",
            "--kind",
            "code",
            "--target-project-id",
            "target",
        ],
    )
    claimed = _coord(
        root,
        ["claim_next_dispatch_intent", "--claimed-by", "failed-worker", "--tier", "senior"],
    )["intent"]
    assert claimed["intent_id"] == original["intent_id"]
    lease = _coord(
        root,
        [
            "open_execution_lease",
            "retained-implementation-lease",
            "--worker-id",
            "failed-worker",
            "--intent-id",
            original["intent_id"],
            "--timeout-seconds",
            "60",
        ],
    )["lease"]
    checkpoint = _coord(
        root,
        [
            "create_execution_checkpoint",
            lease["lease_id"],
            "--reason",
            "supervisor_error",
            "--status",
            "PAUSED",
            "--saga-id",
            saga["saga_id"],
            "--source-repo-path",
            str(target),
            "--base-head-sha",
            base_sha,
        ],
    )["checkpoint"]
    recovery = _coord(
        root,
        [
            "request_recovery_staff_review",
            checkpoint["checkpoint_id"],
            "--target-project-id",
            "target",
            "--branch",
            retained_branch,
            "--base-head-sha",
            base_sha,
            "--commit-sha",
            retained_commit,
            "--milestone-id",
            "milestone-3",
        ],
    )
    assert recovery["next_step"] == "pi /dispatch"

    claude_marker = tmp_path / "senior-was-started"
    claude = tmp_path / "fake_claude.py"
    claude.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        f"Path({str(claude_marker)!r}).write_text('started', encoding='utf-8')\n",
        encoding="utf-8",
    )
    claude.chmod(0o755)
    codex = tmp_path / "fake_codex.py"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:3] == ['login', 'status']:\n"
        "    raise SystemExit(0)\n"
        "if not Path('FEATURE.md').exists():\n"
        "    print('VERDICT: BLOCK - retained implementation is missing')\n"
        "    raise SystemExit(2)\n"
        "print('VERDICT: APPROVE - exact retained implementation reviewed read-only')\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)

    runner = DispatcherIntentRunner(
        runtime,
        delegate_fn=lambda **_kwargs: {"ok": True, "output": "unused", "metadata": {}},
        claude_bin=str(claude),
        codex_bin=str(codex),
    )
    dispatcher = LedgerDispatcher(runner, name="recovery-review", settings=runtime.settings)

    outcome = dispatcher.poll_once()

    assert isinstance(outcome, Dispatched)
    assert outcome.status == "DONE"
    assert not claude_marker.exists()
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=target,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == base_sha
    )
    assert (
        subprocess.run(
            ["git", "rev-parse", retained_branch],
            cwd=target,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == retained_commit
    )

    done = _coord(root, ["list_dispatch_intents", "--status", "DONE"])["intents"]
    recovery_row = next(row for row in done if row["intent_id"] == recovery["intent"]["intent_id"])
    result = json.loads(recovery_row["result"])
    assert result["result_origin"] == "AUTOMATED_RECOVERY"
    assert result["promotion_state"] == "MERGE_PENDING"
    assert [task["task_name"] for task in result["run_result"]["tasks"]] == [
        "recovery_revision_anchor",
        "recovery_staff_review",
    ]
    anchor = result["run_result"]["tasks"][0]
    anchor_evidence = next(
        item["content"]
        for item in anchor["artifacts"]
        if item["artifact_type"] == "recovery_review_anchor"
    )
    assert anchor_evidence["implementation_model_started"] is False
    review_task = result["run_result"]["tasks"][1]
    review = next(
        item["content"]
        for item in review_task["artifacts"]
        if item["artifact_type"] == "review_result"
    )
    assert review == {
        **review,
        "schema_version": "review_result.v1",
        "verdict": "approve",
        "finding_severity": "NON_BLOCKING",
        "review_origin": "RECOVERY_STAFF",
        "reviewer_tier": "STAFF",
        "harness": "codex",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "task_id": result["task_ids_by_name"]["recovery_staff_review"],
        "reviewed_commit_sha": retained_commit,
        "base_sha": base_sha,
        "attempt_number": 1,
        "completion_status": "COMPLETED",
        "engineering_doctrine": CURRENT_ENGINEERING_DOCTRINE.provenance_payload(),
        "provenance_stamped_by": "pow_wow_executor",
    }
    assert review["execution_lease_id"]
    approvals = _coord(root, ["list_approval_requests", "--status", "PENDING"])["requests"]
    assert len(approvals) == 1
    approval = approvals[0]["payload"]
    assert approval["base_sha"] == base_sha
    assert approval["commit_sha"] == retained_commit
    assert approval["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert approval["milestone_id"] == "milestone-3"
    assert approval["dispatch_result"]["result_origin"] == "AUTOMATED_RECOVERY"


def test_tool_gate_denies_unless_allowlisted() -> None:
    gate = ToolGate.from_allowlist(set())  # nothing authorized
    res = gate.execute_allowed_tool(
        "shelly_plug", {"base_url": "http://192.168.1.9", "action": "off"}
    )
    assert res.allowed is False and res.ran is False
    assert "allow-list" in (res.error or "")


def test_tool_gate_runs_allowlisted_tool_with_injected_http() -> None:
    calls = []

    def fake_http(url: str):
        calls.append(url)
        return {"was_on": False}

    gate = ToolGate.from_allowlist({"shelly_plug"})
    gate.tools["shelly_plug"] = lambda **kw: shelly_plug(http_get=fake_http, **kw)
    res = gate.execute_allowed_tool(
        "shelly_plug", {"base_url": "http://192.168.1.9", "action": "off", "channel": 0}
    )
    assert res.allowed and res.ran and res.ok
    assert res.output["action"] == "off"
    assert calls == ["http://192.168.1.9/rpc/Switch.Set?id=0&on=false"]


def test_shelly_plug_builds_on_url() -> None:
    out = shelly_plug(base_url="http://10.0.0.5/", action="on", channel=1, http_get=lambda u: u)
    assert out["response"] == "http://10.0.0.5/rpc/Switch.Set?id=1&on=true"


def test_quorum_submit_expands_children_and_gates_reducer(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    quorum = _coord(
        root,
        [
            "submit_dispatch_intent",
            "senior",
            "Which auth approach should we use?",
            "--fanout",
            "3",
            "--allow-tier",
            "junior",
            "--allow-tier",
            "senior",
            "--allow-tier",
            "staff",
            "--reduce",
            "judge",
        ],
    )
    assert quorum["intent_role"] == "quorum"
    assert len(quorum["child_intent_ids"]) == 3
    assert quorum["reducer_tier"] == "staff"

    # Children claim in decorrelated tier order; the quorum parent is never
    # claimable and the reducer is gated until every child is terminal.
    claimed = [
        _coord(root, ["claim_next_dispatch_intent", "--claimed-by", f"r{i}"])["intent"]
        for i in range(4)
    ]
    assert [c["intent_role"] for c in claimed[:3]] == ["child", "child", "child"]
    assert sorted(c["tier"] for c in claimed[:3]) == ["junior", "senior", "staff"]
    assert claimed[3] is None

    for child in claimed[:3]:
        _coord(
            root,
            [
                "complete_dispatch_intent",
                child["intent_id"],
                "DONE",
                "--result",
                f"answer from {child['tier']}",
            ],
        )
    reducer = _coord(root, ["claim_next_dispatch_intent", "--claimed-by", "rx"])["intent"]
    assert reducer["intent_role"] == "reducer"
    assert reducer["tier"] == "staff"

    done = _coord(
        root,
        ["complete_dispatch_intent", reducer["intent_id"], "DONE", "--result", "reduced"],
    )
    assert done["completed_parent_intent_id"] == quorum["intent_id"]
    rows = _coord(root, ["list_dispatch_intents"])["intents"]
    parent = next(row for row in rows if row["intent_id"] == quorum["intent_id"])
    assert parent["status"] == "DONE"
    assert parent["result"] == "reduced"


def test_quorum_validity_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    invalid_shapes = [
        # fanout > 1 without a reduce
        ["--fanout", "2", "--allow-tier", "junior"],
        # fanout > 1 without allow_tiers
        ["--fanout", "2", "--reduce", "vote"],
        # code quorum has no merge semantics
        ["--fanout", "2", "--reduce", "vote", "--allow-tier", "junior", "--kind", "code"],
        # a single answer cannot be reduced
        ["--reduce", "vote"],
    ]
    for extra in invalid_shapes:
        with pytest.raises(RuntimeError, match="invalid_quorum"):
            _coord(root, ["submit_dispatch_intent", "senior", "x", *extra])


def test_single_intent_with_allow_tiers_is_overflow_claimable(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    intent = _coord(
        root,
        ["submit_dispatch_intent", "senior", "overflow me", "--allow-tier", "junior"],
    )
    assert intent["intent_role"] == "single"
    got = _coord(
        root,
        ["claim_next_dispatch_intent", "--claimed-by", "jr", "--tier", "junior"],
    )["intent"]
    assert got is not None and got["intent_id"] == intent["intent_id"]


def test_cancel_quorum_cascades_to_pending_children(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    quorum = _coord(
        root,
        [
            "submit_dispatch_intent",
            "senior",
            "cancel me",
            "--fanout",
            "2",
            "--allow-tier",
            "junior",
            "--reduce",
            "vote",
        ],
    )
    canceled = _coord(root, ["cancel_dispatch_intent", quorum["intent_id"]])
    assert canceled["ok"] is True
    assert canceled["canceled_children"] == 3  # 2 children + 1 reducer
    rows = _coord(
        root,
        ["list_dispatch_intents", "--parent-intent-id", quorum["intent_id"]],
    )["intents"]
    assert {row["status"] for row in rows} == {"CANCELED"}


def _submit_vote_quorum(root: Path, answers: list[str | None], *, fanout: int) -> dict:
    """Submit a vote quorum and complete children with the given answers.

    None means the child FAILED without a usable answer.
    """
    quorum = _coord(
        root,
        [
            "submit_dispatch_intent",
            "senior",
            "APPROVE or REJECT?",
            "--fanout",
            str(fanout),
            "--allow-tier",
            "junior",
            "--reduce",
            "vote",
        ],
    )
    for answer in answers:
        child = _coord(root, ["claim_next_dispatch_intent", "--claimed-by", "c"])["intent"]
        assert child["intent_role"] == "child"
        if answer is None:
            _coord(
                root,
                [
                    "complete_dispatch_intent",
                    child["intent_id"],
                    "FAILED",
                    "--error",
                    "child crashed",
                ],
            )
        else:
            _coord(
                root,
                ["complete_dispatch_intent", child["intent_id"], "DONE", "--result", answer],
            )
    return quorum


def _run_reducer(root: Path, runtime) -> tuple[dict, tuple]:
    runtime.settings.coordination_root = root
    runner = DispatcherIntentRunner(runtime)
    reducer = _coord(root, ["claim_next_dispatch_intent", "--claimed-by", "reducer"])["intent"]
    assert reducer is not None and reducer["intent_role"] == "reducer"
    return reducer, runner(reducer)


def test_vote_reduce_majority_wins(tmp_path: Path, runtime) -> None:
    root = tmp_path / "coord"
    quorum = _submit_vote_quorum(
        root,
        ["APPROVE\nrationale one", "approve\nother rationale", "REJECT"],
        fanout=3,
    )
    reducer, (status, result, error) = _run_reducer(root, runtime)
    assert status == "DONE" and error is None
    payload = json.loads(result)
    assert payload["schema_version"] == "quorum_reduction.v1"
    assert payload["outcome"] == "majority"
    assert payload["votes"] == 2
    # The winner is a representative of the normalized equivalence class; which
    # original casing survives depends on child ordering.
    assert payload["reduced_answer"].lower().startswith("approve")
    # completing the reducer through the normal ledger call cascades to parent
    _coord(
        root,
        ["complete_dispatch_intent", reducer["intent_id"], status, "--result", result],
    )
    rows = _coord(root, ["list_dispatch_intents"])["intents"]
    parent = next(row for row in rows if row["intent_id"] == quorum["intent_id"])
    assert parent["status"] == "DONE"
    assert json.loads(parent["result"])["outcome"] == "majority"


def test_vote_reduce_tie_and_no_majority_fail_closed(tmp_path: Path, runtime) -> None:
    root = tmp_path / "coord"
    _submit_vote_quorum(root, ["APPROVE", "REJECT"], fanout=2)
    _reducer, (status, result, error) = _run_reducer(root, runtime)
    assert status == "FAILED" and "tie" in error
    assert json.loads(result)["outcome"] == "tie"

    root2 = tmp_path / "coord2"
    _submit_vote_quorum(root2, ["APPROVE", None, None], fanout=3)
    _reducer2, (status2, result2, error2) = _run_reducer(root2, runtime)
    assert status2 == "FAILED" and "1 of 3" in error2
    assert json.loads(result2)["outcome"] == "no_majority"


def test_judge_reduce_completes_parent_end_to_end(tmp_path: Path, runtime) -> None:
    root = tmp_path / "coord"
    target = tmp_path / "target"
    target.mkdir()
    runtime.settings.coordination_root = root
    runtime.settings.saga_worktree_root = tmp_path / "worktrees"
    _write_linked_projects(runtime.settings.config_dir, target)

    class FakeJudgeExecutor:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def dispatch_pow_wow(self, pow_wow_id, target_project, tasks, context):
            self.prompts.append(context.goal)
            task_results = tuple(
                PowWowTaskResult(
                    task_name=task.task_name,
                    role=task.role,
                    status="completed",
                    summary="judge done",
                    artifacts=(
                        PowWowArtifact(
                            artifact_type="cli_agent_run",
                            task_name=task.task_name,
                            content={
                                "schema_version": "cli_agent_run.v1",
                                "output": "Use PostgreSQL with row-level tenancy.",
                            },
                        ),
                    ),
                )
                for task in tasks
            )
            return PowWowRunResult(
                executor="FakeJudgeExecutor",
                mode="cli",
                pow_wow_id=pow_wow_id,
                target_project_id=target_project.id,
                target_project_path=str(target_project.expanded_path),
                status="COMPLETED",
                output_summary="judge synthesized",
                tasks=task_results,
                external_agents_started=True,
                auto_merge=False,
            )

    quorum = _coord(
        root,
        [
            "submit_dispatch_intent",
            "senior",
            "Which persistence approach?",
            "--fanout",
            "2",
            "--allow-tier",
            "junior",
            "--allow-tier",
            "senior",
            "--reduce",
            "judge",
            "--target-project-id",
            "target",
        ],
    )
    for _ in range(2):
        child = _coord(root, ["claim_next_dispatch_intent", "--claimed-by", "c"])["intent"]
        _coord(
            root,
            [
                "complete_dispatch_intent",
                child["intent_id"],
                "DONE",
                "--result",
                f"opinion from {child['tier']}",
            ],
        )

    fake = FakeJudgeExecutor()
    runner = DispatcherIntentRunner(runtime, executor_factory=lambda _bench, _ceiling: fake)  # type: ignore[arg-type]
    dispatcher = LedgerDispatcher(
        runner,
        name="ensemble-reactor",
        settings=runtime.settings,
    )
    outcome = dispatcher.poll_once()
    assert isinstance(outcome, Dispatched)
    assert outcome.status == "DONE"

    # The judge prompt carried both child answers.
    assert "opinion from junior" in fake.prompts[0]
    assert "opinion from senior" in fake.prompts[0]

    rows = _coord(root, ["list_dispatch_intents"])["intents"]
    parent = next(row for row in rows if row["intent_id"] == quorum["intent_id"])
    assert parent["status"] == "DONE"
    reduction = json.loads(parent["result"])
    assert reduction["schema_version"] == "quorum_reduction.v1"
    assert reduction["outcome"] == "judged"
    assert reduction["reduced_answer"] == "Use PostgreSQL with row-level tenancy."
    assert len(reduction["child_answers"]) == 2


def test_dispatch_reads_the_quota_record_before_choosing_a_bench(tmp_path: Path, runtime) -> None:
    """The integration point itself, not the helper behind it.

    Everything else about the quota split is asserted against
    `bench_for_dispatch` directly, so deleting its one call site in `run_intent`
    would leave those tests green while every dispatch went back to the bench
    fixed at construction - the exact regression this feature exists to close.
    This drives a claimed intent through the dispatcher and asserts the executor
    was built with the restaffed bench, which makes the wiring load-bearing.

    The factory raises once it has recorded its input: the executor's own
    behaviour is covered elsewhere, and a runner crash is already proven to fail
    the intent rather than the dispatcher.
    """

    from local_first_agent_os.staffing import DEFAULT_BENCH, Tier

    root = tmp_path / "coord"
    target = tmp_path / "target"
    target.mkdir()
    runtime.settings.coordination_root = root
    runtime.settings.saga_worktree_root = tmp_path / "worktrees"
    _write_linked_projects(runtime.settings.config_dir, target)

    opened = _coord(
        root,
        [
            "open_execution_lease",
            "quota-before-bench",
            "--worker-id",
            f"cli:{DEFAULT_BENCH[Tier.STAFF].harness.value}:"
            "cc33cc33-0000-4000-8000-000000000001:dispatch_x_staff",
            "--agent-tier",
            "staff",
            "--agent-name",
            DEFAULT_BENCH[Tier.STAFF].harness.value,
        ],
    )
    _coord(
        root,
        [
            "complete_execution_lease",
            opened["lease"]["lease_id"],
            "FAILED",
            "--result-json",
            json.dumps({"agent_failure": "USAGE_LIMIT"}),
        ],
    )

    handed_benches: list[dict] = []

    def factory(bench, ceiling):
        handed_benches.append(dict(bench))
        raise RuntimeError("the factory input is this test's whole subject")

    runner = DispatcherIntentRunner(
        runtime,
        bench=dict(DEFAULT_BENCH),
        executor_factory=factory,  # type: ignore[arg-type]
    )
    _coord(
        root,
        [
            "submit_dispatch_intent",
            "senior",
            "a milestone after the limit",
            "--kind",
            "code",
            "--target-project-id",
            "target",
        ],
    )

    dispatcher = LedgerDispatcher(runner, name="quota-dispatcher", settings=runtime.settings)
    outcome = dispatcher.poll_once()

    assert isinstance(outcome, Dispatched)
    assert handed_benches, "run_intent never consulted the quota record"
    # The staff seat's provider reported the spent quota, so the bench handed to
    # the executor moves that tier onto the other vendor while the runner's own
    # bench, fixed at construction, still names the spent one.
    assert handed_benches[0][Tier.STAFF].harness is DEFAULT_BENCH[Tier.SENIOR].harness
    assert runner.bench[Tier.STAFF].harness is DEFAULT_BENCH[Tier.STAFF].harness
