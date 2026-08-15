# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HTTP readiness must not wait on application work.

`local-agent serve` once hung before opening port 8000 because the FastAPI
lifespan replayed pending application `workflow_runs` before its first yield,
and one stale `model_directive` row sat there loading a local model. Recovery
of those rows is an operator action (`local-agent resume-workflows`); DBOS owns
recovery of DBOS executions. Neither belongs in front of readiness.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

from local_first_agent_os.api import create_app
from local_first_agent_os.contracts import (
    SourceType,
    Stage,
    WorkflowStatus,
    WorkflowType,
    WorkspaceId,
)
from local_first_agent_os.ingress import normalize_scheduled_event

_STALLED_WORKFLOW_ID = "model_directive:general:manual:stalled-before-restart:v1"


@pytest.fixture()
def served_app(runtime, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build the real app against the disposable test runtime."""

    monkeypatch.setattr("local_first_agent_os.api.get_runtime", lambda: runtime)
    monkeypatch.setattr("local_first_agent_os.api.get_settings", lambda: runtime.settings)
    monkeypatch.setattr("local_first_agent_os.api.launch_dbos", lambda: None)
    return TestClient(create_app())


@pytest.fixture()
def stalled_workflow_run(runtime) -> str:
    """Leave behind exactly the row that used to block startup.

    Its ingress event is present on purpose: a replay pass would find this row
    replayable and run a model directive, which is the work that never
    finished.
    """

    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/start /qwen"},
    )
    runtime.repository.register_ingress_event(event)
    runtime.repository.start_workflow_run(
        workflow_id=_STALLED_WORKFLOW_ID,
        workflow_type=WorkflowType.MODEL_DIRECTIVE.value,
        workspace_id=WorkspaceId.GENERAL.value,
        input_event_id=event.event_id,
    )
    runtime.repository.update_workflow(
        _STALLED_WORKFLOW_ID, status=WorkflowStatus.PROCESSING, stage=Stage.PROCESSING
    )
    return _STALLED_WORKFLOW_ID


def test_startup_serves_health_with_a_stalled_workflow_run_present(
    stalled_workflow_run: str, served_app: TestClient
) -> None:
    with served_app as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_startup_runs_no_application_workflow(
    stalled_workflow_run: str,
    served_app: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect stated as behavior, not as a call to one named function.

    Readiness is coupled to model work exactly when startup executes an
    application workflow, whatever the code path there is called.
    """

    replayed: list[WorkflowType] = []
    monkeypatch.setattr(
        "local_first_agent_os.dbos_app.run_workflow",
        lambda workflow_type, event: replayed.append(workflow_type),
    )

    with served_app as client:
        client.get("/health")

    assert replayed == []


def test_startup_leaves_pending_workflow_rows_for_the_operator(
    runtime, stalled_workflow_run: str, served_app: TestClient
) -> None:
    """Startup is not a recovery pass, so the stale row is still pending."""

    with served_app as client:
        client.get("/health")

    assert [wid for wid, _, _ in runtime.repository.list_pending_workflow_runs()] == [
        stalled_workflow_run
    ]
    state = runtime.repository.get_workflow_run_state(stalled_workflow_run)
    assert state is not None
    assert state.status == WorkflowStatus.PROCESSING


def test_importing_the_api_opens_no_database_connection() -> None:
    """Importing a module must not reach the production ledger.

    `api.py` used to end in `app = create_app()`. That ran `get_runtime()` at
    import time, so merely importing `local_first_agent_os.api` connected to
    whatever `LOCAL_AGENT_DATABASE_URL` names - which on a developer's machine is
    the durable ledger on 5432, not the disposable test database on 5433 that
    `tests/conftest.py` goes out of its way to arrange.

    The symptom was a collection error rather than a test failure: stop the
    runtime, and every test module that imports the API fails to load, pointing
    at a connection refused with no indication that a *test* had reached for
    production.

    A subprocess, because the module is already imported by the time this file
    runs. Asserting on an import that has happened is a check that cannot fail.
    """

    probe = textwrap.dedent(
        """
        import sqlalchemy
        opened = []
        sqlalchemy.create_engine = (
            lambda *a, **k: opened.append(a[0] if a else k) or (_ for _ in ()).throw(
                AssertionError(f"import opened a database engine: {opened[-1]!r}")
            )
        )
        import local_first_agent_os.api  # noqa: F401
        print("clean")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
