# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from postgres_support import point_store_at_database, postgres_admin_url
from psycopg import sql

from local_first_agent_os.agent_execution_supervisor import StreamingCommandSupervisor
from local_first_agent_os.artifacts import ArtifactStore
from local_first_agent_os.coordination import (
    CoordinationCommand,
    CoordinationResult,
    EntityResult,
    OpenExecutionLease,
)
from local_first_agent_os.db import Database
from local_first_agent_os.merge_review import (
    pending_code_merge_approval,
    review_packet_for_approval,
)
from local_first_agent_os.pow_wow import (
    CliPowWowExecutor,
    PowWowExecutionContext,
    PowWowTaskSpec,
    run_typed_coordination_command,
)
from local_first_agent_os.pow_wow.ledger import run_coordination_command
from local_first_agent_os.pow_wow.types import ExecutionAttemptLease
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.repository import Repository
from local_first_agent_os.settings import Settings
from local_first_agent_os.staffing import JudgmentRole, Tier
from local_first_agent_os.workflow.engine import (
    _find_latest_dependency_ready_gawd,
    _find_latest_retryable_milestone,
)
from local_first_agent_os.workflow.saga_support import (
    resolve_target_project_from_gawd_dispatch_history,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("LOCAL_AGENT_RUN_POSTGRES_INTEGRATION") != "1",
        reason="set LOCAL_AGENT_RUN_POSTGRES_INTEGRATION=1 to run real Postgres tests",
    ),
]


@dataclass(frozen=True)
class _PostgresHarness:
    settings: Settings
    database_url: str
    artifacts: ArtifactStore
    coordinate: Callable[[CoordinationCommand], CoordinationResult]


class _OneRealForeignKeyFailure:
    """Force one real artifacts.workflow_id FK failure, then delegate normally."""

    def __init__(self, wrapped: ArtifactStore) -> None:
        self.wrapped = wrapped
        self.remaining = 1

    def write_text(self, **kwargs: Any) -> Any:
        if self.remaining:
            self.remaining -= 1
            kwargs["workflow_id"] = "missing-frontier-workflow"
        return self.wrapped.write_text(**kwargs)

    def read_text(self, artifact_id: str) -> str:
        return self.wrapped.read_text(artifact_id)


class _FastTerminationSupervisor(StreamingCommandSupervisor):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            **kwargs,
            heartbeat_seconds=0.05,
            warning_seconds=0.1,
            termination_grace_seconds=0.05,
        )


def _psycopg_url(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def _json_object(value: object) -> dict[str, object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    assert isinstance(decoded, dict)
    return decoded


@pytest.fixture()
def postgres_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_PostgresHarness]:
    admin_url = _psycopg_url(postgres_admin_url())
    database_name = f"local_agent_integration_{uuid.uuid4().hex}"
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

    database_url = admin_url.rsplit("/", 1)[0] + f"/{database_name}"
    # The store reads the schema from the environment rather than from Settings,
    # so passing a coordination_database_url here is not enough on its own.
    point_store_at_database(monkeypatch, database_url)
    sqlalchemy_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    settings = Settings.model_validate(
        {
            "database_url": sqlalchemy_url,
            "coordination_backend": "postgres",
            "coordination_database_url": sqlalchemy_url,
            "coordination_root": tmp_path / "coordination",
            "artifact_root": tmp_path / "artifacts",
            "saga_worktree_root": tmp_path / "worktrees",
            "use_dbos": False,
            "mock_models": True,
        }
    )
    database = Database(settings)
    repository = Repository(database)
    repository.create_database_schema()
    artifacts = ArtifactStore(settings.artifact_root, repository, settings)

    def coordinate(command: CoordinationCommand) -> CoordinationResult:
        return run_typed_coordination_command(command, settings=settings)

    try:
        yield _PostgresHarness(
            settings=settings,
            database_url=database_url,
            artifacts=artifacts,
            coordinate=coordinate,
        )
    finally:
        database.engine.dispose()
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=%s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


def _open_lease(
    harness: _PostgresHarness,
    worktree: Path,
    *,
    intent_id: str | None = None,
) -> ExecutionAttemptLease:
    opened = harness.coordinate(
        OpenExecutionLease(
            idempotency_key=f"integration:{uuid.uuid4()}",
            worker_id="integration-worker",
            timeout_seconds=30,
            agent_tier="senior",
            agent_name="claude",
            intent_id=intent_id,
            worktree_path=str(worktree),
            command=(sys.executable, "-c", "pass"),
        )
    )
    assert isinstance(opened, EntityResult)
    lease_id = str(opened.entity.values["lease_id"])
    return ExecutionAttemptLease(
        idempotency_key="integration-supervisor",
        worker_id="integration-worker",
        lease_id=lease_id,
        created=True,
        open_status="ACTIVE",
    )


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "integration@example.com"],
        ["git", "config", "user.name", "Integration Test"],
    ):
        subprocess.run(command, cwd=path, check=True, capture_output=True, text=True)
    (path / "README.md").write_text("# integration\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "initial"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_real_postgres_artifact_fk_accepts_supervisor_transcript(
    postgres_harness: _PostgresHarness, tmp_path: Path
) -> None:
    repo = tmp_path / "supervised-repo"
    _init_git_repo(repo)
    lease = _open_lease(postgres_harness, repo)
    supervisor = StreamingCommandSupervisor(
        coordination_command=postgres_harness.coordinate,
        artifact_writer=postgres_harness.artifacts,
        heartbeat_seconds=0.02,
    )
    code = "import json; print(json.dumps({'type':'result','result':'ok'}), flush=True)"

    result = asyncio.run(
        supervisor.run(
            [sys.executable, "-u", "-c", code],
            repo,
            lease=lease,
            harness="claude",
            timeout_seconds=5,
        )
    )

    assert result.capture.exit_code == 0
    assert result.transcript_artifact_id
    with psycopg.connect(postgres_harness.database_url) as connection:
        row = connection.execute(
            "SELECT workflow_id, role FROM artifacts WHERE artifact_id=%s",
            (result.transcript_artifact_id,),
        ).fetchone()
        execution_link = connection.execute(
            "SELECT lease_id, artifact_id, role FROM agent_execution_artifacts "
            "WHERE artifact_id=%s",
            (result.transcript_artifact_id,),
        ).fetchone()
    assert row == (None, "agent_execution_transcript")
    assert execution_link == (
        lease.lease_id,
        result.transcript_artifact_id,
        "agent_execution_transcript",
    )


def test_real_postgres_sigkill_recovery_completes_lease_and_releases_intent(
    postgres_harness: _PostgresHarness, tmp_path: Path
) -> None:
    repo = tmp_path / "sigkill-repo"
    _init_git_repo(repo)
    intent = run_coordination_command(
        [
            "submit_dispatch_intent",
            "staff",
            "Exercise the SIGKILL checkpoint boundary.",
            "--kind",
            "code",
            "--target-project-id",
            "integration-target",
        ],
        settings=postgres_harness.settings,
    )
    claimed = run_coordination_command(
        ["claim_next_dispatch_intent", "--claimed-by", "integration-worker", "--tier", "staff"],
        settings=postgres_harness.settings,
    )
    assert claimed["intent"]["intent_id"] == intent["intent_id"]
    lease = _open_lease(postgres_harness, repo, intent_id=intent["intent_id"])
    executor = CliPowWowExecutor(
        worktree_root=postgres_harness.settings.saga_worktree_root,
        timeout_seconds=1,
        coordination_command=postgres_harness.coordinate,
        artifact_writer=postgres_harness.artifacts,
        supervisor_factory=_FastTerminationSupervisor,
    )
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    escaped_pid_path = repo / "escaped-child.pid"
    escaped_code = "import time; time.sleep(30)"
    code = (
        "from pathlib import Path; import signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path('README.md').write_text('# timed out but preserved\\n'); "
        f"child=subprocess.Popen([sys.executable,'-u','-c',{escaped_code!r}], "
        "start_new_session=True); "
        "Path('escaped-child.pid').write_text(str(child.pid)); "
        "print('parent ready', flush=True); time.sleep(30)"
    )

    try:
        capture, supervised = executor._run_frontier_command(
            [sys.executable, "-u", "-c", code],
            repo,
            execution_attempt=lease,
            harness="codex",
            env=None,
            source_repo_path=repo,
            base_head_sha=base_sha,
            saga_id="integration-saga",
            pow_wow_id="integration-pow-wow",
            task_contract="escaped descendant must not pin process.wait",
        )
    finally:
        if escaped_pid_path.exists():
            escaped_pid = int(escaped_pid_path.read_text(encoding="utf-8"))
            with contextlib.suppress(ProcessLookupError):
                os.kill(escaped_pid, signal.SIGKILL)
    assert supervised is not None
    executor._complete_execution_attempt_lease(
        lease,
        capture=capture,
        dirty_worktree={
            "schema_version": "dirty_worktree.v1",
            "source_repo_path": str(repo),
            "worktree_path": str(repo),
            "head_sha": base_sha,
            "changed_files": ["README.md"],
            "cleanup_policy": "preserve",
            "cleanup_requested": False,
            "cleanup_applied": False,
        },
        supervised_result=supervised,
    )

    with psycopg.connect(postgres_harness.database_url) as connection:
        lease_row = connection.execute(
            "SELECT status, agent_failure, supervisor_status, persistence_status "
            "FROM agent_execution_leases WHERE lease_id=%s",
            (lease.lease_id,),
        ).fetchone()
        intent_status = connection.execute(
            "SELECT status FROM dispatch_intents WHERE intent_id=%s",
            (intent["intent_id"],),
        ).fetchone()
        kinds = [
            row[0]
            for row in connection.execute(
                "SELECT kind FROM agent_execution_events WHERE lease_id=%s ORDER BY sequence",
                (lease.lease_id,),
            ).fetchall()
        ]
        artifact_roles = {
            row[0]
            for row in connection.execute(
                "SELECT role FROM agent_execution_artifacts WHERE lease_id=%s",
                (lease.lease_id,),
            ).fetchall()
        }
        checkpoint_count = connection.execute(
            "SELECT count(*) FROM agent_execution_checkpoints WHERE lease_id=%s",
            (lease.lease_id,),
        ).fetchone()

    assert capture.exit_code == 124
    assert supervised.checkpoint_id
    assert lease_row == ("TIMED_OUT", "DEADLINE_EXCEEDED", "COMPLETED", "COMPLETED")
    assert intent_status == ("CHECKPOINT_REVIEW",)
    assert kinds.count("process.sigkill") == 1
    assert kinds.count("process.wait_abandoned") == 1
    assert kinds.index("process.sigkill") < kinds.index("process.exited")
    assert kinds.index("process.exited") < kinds.index("agent.finished")
    assert artifact_roles == {
        "agent_execution_transcript",
        "agent_checkpoint_git_status",
        "agent_checkpoint_patch",
        "agent_checkpoint_test_summary",
    }
    assert checkpoint_count == (1,)


def test_real_postgres_separates_lease_ownership_from_progress_health(
    postgres_harness: _PostgresHarness, tmp_path: Path
) -> None:
    repo = tmp_path / "stalled-repo"
    _init_git_repo(repo)
    lease = _open_lease(postgres_harness, repo)
    assessments: list[dict[str, object]] = []

    def assess(evidence: Mapping[str, object]) -> dict[str, object]:
        assessments.append(dict(evidence))
        return {
            "recommendation": "CONTINUE",
            "rationale": "Bounded integration-test process.",
            "continuations": [],
        }

    supervisor = StreamingCommandSupervisor(
        coordination_command=postgres_harness.coordinate,
        artifact_writer=postgres_harness.artifacts,
        heartbeat_seconds=0.1,
        quiet_seconds=0.2,
        stalled_seconds=0.4,
        progress_assessor=assess,
    )
    result = asyncio.run(
        supervisor.run(
            [sys.executable, "-u", "-c", "import time; time.sleep(3)"],
            repo,
            lease=lease,
            harness="codex",
            timeout_seconds=6,
            task_contract="postgres stall visibility proof",
        )
    )

    assert result.capture.exit_code == 0
    assert result.progress_recommendation == "CONTINUE"
    assert len(assessments) == 1
    with psycopg.connect(postgres_harness.database_url) as connection:
        projection = connection.execute(
            "SELECT status, activity_status, progress_assessment_status, "
            "progress_assessment_decision_json, last_meaningful_progress_sequence "
            "FROM agent_execution_leases WHERE lease_id=%s",
            (lease.lease_id,),
        ).fetchone()
        kinds = [
            row[0]
            for row in connection.execute(
                "SELECT kind FROM agent_execution_events WHERE lease_id=%s ORDER BY sequence",
                (lease.lease_id,),
            ).fetchall()
        ]
    assert projection is not None
    assert projection[0:3] == ("ACTIVE", "STALLED_SUSPECTED", "COMPLETED")
    assert _json_object(projection[3])["recommendation"] == "CONTINUE"
    assert projection[4] is not None
    assert "lease.heartbeat" in kinds
    assert kinds.index("activity.quiet") < kinds.index("activity.stalled_suspected")


def test_real_postgres_claude_limit_switches_to_supervised_codex_lease(
    postgres_harness: _PostgresHarness, tmp_path: Path
) -> None:
    repo = tmp_path / "fallback-repo"
    _init_git_repo(repo)
    claude = tmp_path / "claude"
    claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type':'rate_limit_event','rate_limit_info':"
        "{'status':'rejected','rateLimitType':'five_hour'}}), flush=True)\n"
        "print(json.dumps({'type':'result','is_error':True,'api_error_status':429,"
        "'result':'session limit'}), flush=True)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    codex = tmp_path / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "from pathlib import Path\n"
        "Path('fallback-created.txt').write_text('codex replacement\\n')\n"
        "print(json.dumps({'type':'item.completed','item':"
        "{'type':'agent_message','text':'replacement complete'}}), flush=True)\n",
        encoding="utf-8",
    )
    claude.chmod(0o755)
    codex.chmod(0o755)
    target = LinkedProject(
        id="integration-target",
        kind="test",
        path=repo,
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="Postgres integration target",
        verification_commands=["test -f fallback-created.txt"],
    )
    context = PowWowExecutionContext(
        saga_id="integration-saga",
        goal="exercise provider fallback",
        directive="integration fallback test",
        target_project_id=target.id,
        target_project_path=str(repo),
        target_project_kind=target.kind,
        target_project_status=target.status,
        target_project_read_only=False,
        verification_commands=tuple(target.verification_commands),
    )
    task = PowWowTaskSpec(
        task_name="integration_implementation",
        role="implementer",
        judgment=JudgmentRole(name="implementer", tier=Tier.SENIOR),
        dispatch_kind="code",
        description="Create the fallback proof file.",
    )
    artifact_writer = _OneRealForeignKeyFailure(postgres_harness.artifacts)
    executor = CliPowWowExecutor(
        worktree_root=postgres_harness.settings.saga_worktree_root,
        coordination_command=postgres_harness.coordinate,
        artifact_writer=artifact_writer,
        claude_bin=str(claude),
        codex_bin=str(codex),
    )
    executor._codex_auth_ok_cache = True

    run = executor.dispatch_pow_wow("integration-pow-wow", target, (task,), context)

    assert run.status == "COMPLETED", [
        (item.status, item.summary, item.risks, [a.to_payload() for a in item.artifacts])
        for item in run.tasks
    ]
    with psycopg.connect(postgres_harness.database_url) as connection:
        leases = connection.execute(
            "SELECT lease_id, agent_name, status, outcome, result_json, "
            "agent_status, agent_failure_category, agent_failure, "
            "supervisor_status, supervisor_failure, persistence_status, "
            "persistence_failure, next_action "
            "FROM agent_execution_leases ORDER BY created_at"
        ).fetchall()
        transcript_rows = connection.execute(
            "SELECT workflow_id FROM artifacts WHERE role='agent_execution_transcript'"
        ).fetchall()
        transition = connection.execute(
            "SELECT payload_json FROM agent_execution_events WHERE kind='provider_fallback.started'"
        ).fetchone()
        artifact_links = connection.execute(
            "SELECT lease_id, role FROM agent_execution_artifacts ORDER BY created_at"
        ).fetchall()
        claude_events = connection.execute(
            "SELECT kind FROM agent_execution_events WHERE lease_id=%s ORDER BY sequence",
            (leases[0][0],),
        ).fetchall()

    assert len(leases) == 2
    claude_lease, codex_lease = leases
    assert claude_lease[1:4] == ("claude", "FAILED", "USAGE_LIMIT")
    assert _json_object(claude_lease[4])["next_action"] == "SWITCH_TO_FALLBACK"
    assert claude_lease[5:] == (
        "FAILED",
        "INFRASTRUCTURE",
        "USAGE_LIMIT",
        "COMPLETED",
        None,
        "FAILED",
        "DATA_INTEGRITY_VIOLATION",
        "SWITCH_TO_FALLBACK",
    )
    assert codex_lease[1:4] == ("codex", "COMPLETED", "AUTOMATED_COMPLETION")
    assert codex_lease[5:] == (
        "COMPLETED",
        None,
        None,
        "COMPLETED",
        None,
        "COMPLETED",
        None,
        None,
    )
    assert transcript_rows == [(None,)]
    assert artifact_links == [(codex_lease[0], "agent_execution_transcript")]
    claude_event_kinds = [row[0] for row in claude_events]
    assert claude_event_kinds.index("agent.finished") < claude_event_kinds.index(
        "artifact.persist.failed"
    )
    assert claude_event_kinds.index("artifact.persist.failed") < claude_event_kinds.index(
        "provider_fallback.started"
    )
    assert transition is not None
    transition_payload = _json_object(transition[0])
    assert transition_payload["action"] == "SWITCH_TO_FALLBACK"
    assert transition_payload["replacement_lease_id"] == codex_lease[0]


def test_real_postgres_operator_shortcuts_resolve_from_ledger_state(
    postgres_harness: _PostgresHarness,
) -> None:
    settings = postgres_harness.settings
    saga = run_coordination_command(
        ["create_saga", "Postgres shortcut integration"], settings=settings
    )
    doc = run_coordination_command(
        [
            "create_gawd_doc",
            "Postgres shortcut integration",
            "--saga-id",
            saga["saga_id"],
            "--task-graph-json",
            "{}",
        ],
        settings=settings,
    )
    run_coordination_command(
        ["attach_gawd_doc_to_saga", saga["saga_id"], doc["gawd_doc_id"]],
        settings=settings,
    )
    run_coordination_command(
        [
            "create_saga_milestone",
            saga["saga_id"],
            "Retryable",
            "--sequence",
            "1",
            "--milestone-id",
            "postgres-retryable",
            "--gawd-doc-id",
            doc["gawd_doc_id"],
        ],
        settings=settings,
    )
    run_coordination_command(
        ["fail_saga_milestone", "postgres-retryable", "provider exhausted"],
        settings=settings,
    )
    historical_intent = run_coordination_command(
        [
            "submit_dispatch_intent",
            "senior",
            "Historical target provenance",
            "--kind",
            "code",
            "--target-project-id",
            "integration-target",
            "--source",
            f"approved_gawd:{doc['gawd_doc_id']}:milestone:prior",
        ],
        settings=settings,
    )

    retry_resolution = _find_latest_retryable_milestone(settings)
    assert retry_resolution["milestone_id"] == "postgres-retryable"
    assert resolve_target_project_from_gawd_dispatch_history(settings, doc["gawd_doc_id"]) == {
        "target_project_id": "integration-target",
        "source": "prior_gawd_dispatch_intents",
        "intent_ids": [historical_intent["intent_id"]],
    }
    run_coordination_command(
        [
            "retry_saga_milestone",
            "postgres-retryable",
            "integration retry",
        ],
        settings=settings,
    )

    approval_resolution = _find_latest_dependency_ready_gawd(settings)
    assert approval_resolution["saga_id"] == saga["saga_id"]
    assert approval_resolution["gawd_doc_id"] == doc["gawd_doc_id"]
    assert approval_resolution["milestone_id"] == "postgres-retryable"


def test_real_postgres_legacy_merge_approval_hydrates_without_mutation(
    postgres_harness: _PostgresHarness,
    tmp_path: Path,
) -> None:
    settings = postgres_harness.settings
    repo = tmp_path / "merge-review-repo"
    _init_git_repo(repo)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "feature.py").write_text("READY = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "review feature"], cwd=repo, check=True)
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    saga = run_coordination_command(["create_saga", "Postgres merge review"], settings=settings)
    intent = run_coordination_command(
        [
            "submit_dispatch_intent",
            "senior",
            "Build a reviewed feature",
            "--kind",
            "code",
            "--target-project-id",
            "integration-target",
        ],
        settings=settings,
    )
    run_coordination_command(
        ["claim_next_dispatch_intent", "--claimed-by", "postgres-review"],
        settings=settings,
    )
    result = {
        "schema_version": "dispatch_runner_result.v1",
        "run_result": {
            "pow_wow_id": "postgres-review-pow",
            "target_project_id": "integration-target",
            "target_project_path": str(repo),
            "status": "COMPLETED",
            "output_summary": "Postgres-backed run completed.",
            "changed_files": ["feature.py"],
            "verification_commands": ["pytest -q"],
            "verification_output": ["pytest -q -> 0\n1 passed"],
            "risks": [],
            "tasks": [
                {
                    "task_name": "implementation",
                    "role": "implementer",
                    "status": "completed",
                    "summary": "Implemented.",
                    "risks": [],
                    "artifacts": [
                        {
                            "artifact_type": "worktree_commit_checkpoint",
                            "task_name": "implementation",
                            "content": {
                                "branch_name": "main",
                                "base_head_sha": base_sha,
                                "commit_sha": commit_sha,
                                "commit_created": True,
                                "changed_from_base": True,
                                "checkpointed_files": ["feature.py"],
                            },
                        }
                    ],
                }
            ],
            "artifacts": [],
            "external_agents_started": True,
            "auto_merge": False,
        },
    }
    run_coordination_command(
        [
            "complete_dispatch_intent",
            intent["intent_id"],
            "DONE",
            "--result",
            json.dumps(result),
        ],
        settings=settings,
    )
    submitted = run_coordination_command(
        [
            "submit_approval_request",
            saga["saga_id"],
            "CODE_MERGE",
            "--payload",
            json.dumps(
                {
                    "intent_id": intent["intent_id"],
                    "target_project_id": "integration-target",
                    "changed_files": ["feature.py"],
                }
            ),
        ],
        settings=settings,
    )

    approval = pending_code_merge_approval(settings=settings)
    packet = review_packet_for_approval(approval, settings=settings)

    assert approval["approval_id"] == submitted["approval_id"]
    assert packet["changed_files"] == ["feature.py"]
    assert "feature.py" in packet["diffs"][0]["diff_stat"]
    with psycopg.connect(postgres_harness.database_url) as connection:
        status = connection.execute(
            "SELECT status FROM approval_requests WHERE approval_id=%s",
            (submitted["approval_id"],),
        ).fetchone()
    assert status == ("PENDING",)


def test_real_postgres_recovery_staff_review_request_is_atomic_and_typed(
    postgres_harness: _PostgresHarness,
) -> None:
    settings = postgres_harness.settings
    saga = run_coordination_command(
        ["create_saga", "Postgres recovery staff review"], settings=settings
    )
    original = run_coordination_command(
        [
            "submit_dispatch_intent",
            "senior",
            "Retained implementation",
            "--kind",
            "code",
            "--target-project-id",
            "integration-target",
        ],
        settings=settings,
    )
    claimed = run_coordination_command(
        [
            "claim_next_dispatch_intent",
            "--claimed-by",
            "postgres-recovery",
            "--tier",
            "senior",
        ],
        settings=settings,
    )["intent"]
    assert claimed["intent_id"] == original["intent_id"]
    lease = run_coordination_command(
        [
            "open_execution_lease",
            "postgres-recovery-lease",
            "--worker-id",
            "postgres-recovery",
            "--intent-id",
            original["intent_id"],
            "--timeout-seconds",
            "60",
        ],
        settings=settings,
    )["lease"]
    base_sha = "a" * 40
    checkpoint = run_coordination_command(
        [
            "create_execution_checkpoint",
            lease["lease_id"],
            "--reason",
            "supervisor_error",
            "--status",
            "PAUSED",
            "--saga-id",
            saga["saga_id"],
            "--base-head-sha",
            base_sha,
        ],
        settings=settings,
    )["checkpoint"]
    command = [
        "request_recovery_staff_review",
        checkpoint["checkpoint_id"],
        "--target-project-id",
        "integration-target",
        "--branch",
        "agent/retained",
        "--base-head-sha",
        base_sha,
        "--commit-sha",
        "b" * 40,
        "--milestone-id",
        "milestone-3",
    ]

    created = run_coordination_command(command, settings=settings)
    replay = run_coordination_command(command, settings=settings)

    assert created["created"] is True
    assert replay["created"] is False
    assert replay["intent"]["intent_id"] == created["intent"]["intent_id"]
    assert created["intent"]["tier"] == "staff"
    assert created["intent"]["kind"] == "code"
    assert created["intent"]["target_project_id"] == "integration-target"
    payload = json.loads(created["intent"]["prompt"])
    assert payload == {
        "schema_version": "recovery_staff_review_request.v1",
        "checkpoint_id": checkpoint["checkpoint_id"],
        "saga_id": saga["saga_id"],
        "target_project_id": "integration-target",
        "branch": "agent/retained",
        "base_sha": base_sha,
        "commit_sha": "b" * 40,
        "milestone_id": "milestone-3",
        "review_origin": "RECOVERY_STAFF",
        "permission_envelope": "read-only review; revisions only after BLOCK",
    }
