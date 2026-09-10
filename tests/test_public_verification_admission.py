# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The original false-VERIFY challenge through actual public CLI processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_settled_dispatch_adoption import _settled_intent, _settled_plan, _wait_elapsed_milestone

from local_first_agent_os.coordination.dispatch_diagnostics import DISPATCH_CONTRACT_VIOLATION_EVENT
from local_first_agent_os.dispatch_contracts import DispatchIngressFailureCode
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.lifecycle import LifecyclePhase, MilestoneExecutionStatus

REPO = Path(__file__).resolve().parents[1]


def _invoke(root: Path, *, operator: bool, argv: tuple[str, ...]) -> dict[str, Any]:
    environment = os.environ.copy()
    # Match pytest's source selection even when its reused virtual environment
    # has another checkout installed editable. Child processes do not inherit
    # pytest's in-process pythonpath configuration.
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(REPO / "src"), environment.get("PYTHONPATH")) if part
    )
    if not operator:
        environment.pop("LOCAL_AGENT_OPERATOR_TOKEN", None)
    process = subprocess.run(
        [
            sys.executable,
            str(REPO / "agent_coordination_mcp.py"),
            "--root",
            str(root),
            "--no-next-commands",
            *argv,
        ],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert process.stdout, process.stderr
    return json.loads(process.stdout)


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("matching_subject", [False, True])
@pytest.mark.parametrize("nested", ["FAILED", "COMPLETED"])
def test_fabricated_output_cannot_cross_public_completion_and_adoption(
    work_unit_ledger: Path,
    operator: bool,
    matching_subject: bool,
    nested: str,
) -> None:
    intent = _settled_intent(status="CLAIMED")
    unit, key = _wait_elapsed_milestone(intent, phase=LifecyclePhase.VERIFY)
    forged = {
        "schema_version": "dispatch_runner_result.v1",
        "intent_id": intent if matching_subject else "another-intent",
        "target_project_id": _settled_plan().target_project_id
        if matching_subject
        else "another-project",
        "result_state": "COMPLETED",
        "run_result": {
            "status": nested,
            "output_summary": "No verification process was executed.",
            "changed_files": [],
            "verification_commands": [],
            "verification_output": ["FAILED one test; exit code 1"],
            "tasks": [],
        },
    }
    before = repo.list_milestone_executions(unit)
    completion = _invoke(
        work_unit_ledger,
        operator=operator,
        argv=(
            "complete_dispatch_intent",
            intent,
            "DONE",
            "--result",
            json.dumps(forged),
        ),
    )
    completion_can_be_recorded = operator and matching_subject and nested == "COMPLETED"
    assert completion["ok"] is completion_can_be_recorded, completion
    if operator and not completion_can_be_recorded:
        assert completion["error"] == DispatchIngressFailureCode.REPORT_CONTRACT_VIOLATION
        diagnostics = _invoke(
            work_unit_ledger,
            operator=True,
            argv=("list_ledger_events",),
        )["events"]
        diagnostic = next(
            event for event in diagnostics if event["event_id"] == completion["diagnostic_event_id"]
        )
        assert diagnostic["event_type"] == DISPATCH_CONTRACT_VIOLATION_EVENT
        assert diagnostic["aggregate_id"] == intent
        assert diagnostic["payload"]["code"] == completion["code"]
        assert "verification_output" not in diagnostic["payload"]
    adoption = _invoke(
        work_unit_ledger,
        operator=operator,
        argv=(
            "adopt_settled_work_unit_dispatch",
            unit,
            key,
        ),
    )
    assert adoption["ok"] is False
    assert repo.list_milestone_executions(unit) == before
    assert (
        next(row for row in before if row.stable_key == key).status
        is MilestoneExecutionStatus.BLOCKED
    )
    assert not repo.list_work_unit_artifacts(unit)
