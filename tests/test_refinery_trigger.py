# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from fastapi.testclient import TestClient
from refinery_support import build_stack_repository, write_registry_config

import local_first_agent_os.api as api_module
from local_first_agent_os.coordination.approvals import (
    resolve_approval_request,
    submit_approval_request,
)
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.refinery.trigger import (
    IntegrationAccepted,
    IntegrationBlocked,
    plan_integration_trigger,
)
from local_first_agent_os.settings import get_settings


def test_only_an_approved_queued_code_merge_is_accepted(tmp_path, monkeypatch) -> None:
    repository = build_stack_repository(
        tmp_path / "target",
        {"alpha": {"alpha.py": "ALPHA = 1\n"}},
    )
    write_registry_config(tmp_path / "configs", repository.path, project_id="target")
    monkeypatch.setenv("LOCAL_AGENT_CONFIG_DIR", str(tmp_path / "configs"))
    get_settings.cache_clear()
    saga_id = str(create_saga("Land approved work")["saga_id"])
    submitted = submit_approval_request(
        saga_id,
        "CODE_MERGE",
        payload={
            "target_project_id": "target",
            "branch": "agent/alpha",
            "base_sha": repository.base_sha,
            "commit_sha": repository.sha("alpha"),
            "intent_id": "intent-alpha",
            "pow_wow_id": "pow-alpha",
            "milestone_id": "milestone-alpha",
            "changed_files": ["alpha.py"],
        },
        requested_by="dispatcher_runner",
    )
    approval_id = str(submitted["approval_id"])

    before = plan_integration_trigger(approval_id)
    resolution = resolve_approval_request(approval_id, approved=True, resolved_by="operator")
    after = plan_integration_trigger(approval_id)

    assert isinstance(before, IntegrationBlocked)
    assert "PENDING" in before.message
    assert resolution["ok"] is True
    assert isinstance(after, IntegrationAccepted)
    assert after.request_id == resolution["integration_request_id"]
    assert after.target_project_id == "target"


def test_an_accepted_trigger_runs_one_bounded_background_drain(
    runtime,
    monkeypatch,
) -> None:
    accepted = IntegrationAccepted(
        approval_id="approval-1",
        request_id="request-1",
        target_project_id="target",
    )
    calls: list[tuple[str, int | None]] = []
    monkeypatch.setattr(api_module, "get_settings", lambda: runtime.settings)
    monkeypatch.setattr(api_module, "get_runtime", lambda: runtime)
    monkeypatch.setattr(api_module, "plan_integration_trigger", lambda _approval_id: accepted)
    monkeypatch.setattr(
        api_module,
        "run_refinery",
        lambda project_id, max_polls: calls.append((project_id, max_polls)),
    )

    with TestClient(api_module.create_app()) as client:
        response = client.post("/approvals/approval-1/integration")

    assert response.status_code == 200
    assert response.json()["state"] == "accepted"
    assert calls == [("target", 1)]


def test_a_blocked_trigger_never_starts_the_refinery(runtime, monkeypatch) -> None:
    blocked = IntegrationBlocked(
        approval_id="approval-1",
        request_id="request-1",
        target_project_id="target",
        message="The request was parked.",
    )
    calls: list[str] = []
    monkeypatch.setattr(api_module, "get_settings", lambda: runtime.settings)
    monkeypatch.setattr(api_module, "get_runtime", lambda: runtime)
    monkeypatch.setattr(api_module, "plan_integration_trigger", lambda _approval_id: blocked)
    monkeypatch.setattr(
        api_module,
        "run_refinery",
        lambda project_id, max_polls: calls.append(project_id),
    )

    with TestClient(api_module.create_app()) as client:
        response = client.post("/approvals/approval-1/integration")

    assert response.json() == blocked.model_dump(mode="json")
    assert calls == []


def test_reading_integration_status_never_starts_the_refinery(runtime, monkeypatch) -> None:
    accepted = IntegrationAccepted(
        approval_id="approval-1",
        request_id="request-1",
        target_project_id="target",
    )
    calls: list[str] = []
    monkeypatch.setattr(api_module, "get_settings", lambda: runtime.settings)
    monkeypatch.setattr(api_module, "get_runtime", lambda: runtime)
    monkeypatch.setattr(api_module, "plan_integration_trigger", lambda _approval_id: accepted)
    monkeypatch.setattr(
        api_module,
        "run_refinery",
        lambda project_id, max_polls: calls.append(project_id),
    )

    with TestClient(api_module.create_app()) as client:
        response = client.get("/approvals/approval-1/integration")

    assert response.json() == accepted.model_dump(mode="json")
    assert calls == []
