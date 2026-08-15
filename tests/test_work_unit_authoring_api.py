# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from work_unit_support import ACCEPTANCE_DESIGN_DOC

from local_first_agent_os import api as api_module
from local_first_agent_os.api import create_app
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import service


@pytest.fixture()
def authoring_client(
    runtime: Any,
    work_unit_ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setattr(api_module, "get_runtime", lambda: runtime)
    monkeypatch.setattr(api_module, "get_settings", lambda: runtime.settings)
    monkeypatch.setattr(api_module, "launch_dbos", lambda: None)
    monkeypatch.setattr(api_module, "resolve_project_repo_root", lambda: work_unit_ledger)
    return TestClient(create_app())


def test_walkthrough_is_a_typed_state_machine_and_returns_the_finished_document(
    authoring_client: TestClient,
) -> None:
    started = authoring_client.post(
        "/authoring/walkthroughs",
        json={"operation_id": "start-1"},
    )
    assert started.status_code == 200, started.text
    payload = started.json()
    assert payload["state"] == "awaiting_answer"
    walkthru_id = payload["walkthru_id"]

    for index in range(payload["total_sections"]):
        skipped = authoring_client.post(
            f"/authoring/walkthroughs/{walkthru_id}/transitions",
            json={"action": "skip", "operation_id": f"skip-{index}"},
        )
        assert skipped.status_code == 200, skipped.text
        payload = skipped.json()

    assert payload["state"] == "ready_to_finish"
    finished = authoring_client.post(
        f"/authoring/walkthroughs/{walkthru_id}/transitions",
        json={"action": "finish", "operation_id": "finish-1"},
    )
    assert finished.status_code == 200, finished.text
    payload = finished.json()
    assert payload["state"] == "finished"
    assert payload["draft_content"].startswith("# THE GAWD DOC - Mini")


def test_compile_and_start_are_cockpit_facing_typed_routes(
    authoring_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = authoring_client.post(
        "/authoring/design-docs/compile",
        json={
            "design_doc_id": "cockpit-authoring",
            "raw_content": ACCEPTANCE_DESIGN_DOC,
        },
    )
    assert compiled.status_code == 200, compiled.text
    compile_payload = compiled.json()
    assert compile_payload["runnable"] is True
    assert compile_payload["delivery_contract"] == {
        "kind": "required",
        "artifact_types": ["delivery_record"],
        "reason": None,
    }

    captured: dict[str, Any] = {}

    def fake_start(
        compiled_plan_revision_id: str,
        *,
        title: str | None,
        approved_plan_hash: str | None,
    ) -> dict[str, Any]:
        captured.update(
            revision=compiled_plan_revision_id,
            title=title,
            approved_plan_hash=approved_plan_hash,
        )
        return {
            "work_unit_id": "wu-cockpit",
            "root_workflow_id": "work-unit:wu-cockpit",
            "status": "QUEUED",
            "created": True,
            "dispatch": [],
        }

    monkeypatch.setattr(api_module.work_units, "start_work_unit", fake_start)
    started = authoring_client.post(
        "/work-units",
        json={
            "compiled_plan_revision_id": compile_payload["compiled_plan_revision_id"],
            "approved_plan_hash": compile_payload["plan_hash"],
            "title": "Cockpit run",
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["work_unit_id"] == "wu-cockpit"
    assert captured["approved_plan_hash"] == compile_payload["plan_hash"]


def test_gated_permission_policy_requires_approval_of_the_exact_plan_hash(
    work_unit_ledger: Path,
) -> None:
    document = (
        ACCEPTANCE_DESIGN_DOC
        + """

## Permission Envelope

Autonomous permissions:
- read_repo_context
- write_ledger_artifacts

Requested permissions:
- test_command_execution: needed to verify the change
- code_worktree_write: needed to land the change

Denied without explicit approval:
- deploy
"""
    )
    compiled = service.compile_design_doc_text(
        document,
        design_doc_id="gated-start",
    )
    assert compiled.compiled_plan_revision_id is not None
    assert compiled.plan_hash is not None

    with pytest.raises(repo.WorkUnitError, match="approve its exact compiled hash"):
        service.start_work_unit(
            compiled.compiled_plan_revision_id,
            delivery=None,
        )

    started = service.start_work_unit(
        compiled.compiled_plan_revision_id,
        approved_plan_hash=compiled.plan_hash,
        delivery=None,
    )
    assert started["created"] is True


def test_cli_decision_surface_launches_dbos_before_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from local_first_agent_os import dbos_app
    from local_first_agent_os.work_units import commands

    calls: list[str] = []
    monkeypatch.setattr(dbos_app, "launch_dbos", lambda: calls.append("launch"))
    monkeypatch.setattr(
        commands.service,
        "submit_work_unit_decision",
        lambda *_args, **_kwargs: (
            calls.append("record")
            or {
                "work_unit_id": "wu",
                "request_id": "req",
                "decision": "APPROVED",
                "applied": True,
                "milestone_key": "m",
                "sequence_number": 1,
            }
        ),
    )

    result = commands.submit_work_unit_decision("wu", "req", "APPROVED", "idem")

    assert result["ok"] is True
    assert calls == ["launch", "record"]
