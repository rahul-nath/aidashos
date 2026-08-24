# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""An inline delivery is an answer, never a DBOS stack trace.

`agent-ledger resume_work_unit <id> --inline` runs in a one-shot process that
constructs DBOS at import and never launches it. The old inline fallback then
called the `@dbos_workflow`-decorated root directly, and DBOS raised `System
database accessed before DBOS was launched` (observed live 2026-08-23). These
pin the three inline states: a launchable runtime is launched and drives
durably under the derived workflow ID, an unlaunchable one refuses with a
reason, and the identity-decorator world still runs plainly.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from local_first_agent_os import dbos_app
from local_first_agent_os.work_units import root_workflow
from local_first_agent_os.work_units.execution_recovery import execution_workflow_id
from local_first_agent_os.work_units.root_workflow import EnqueueDelivery


def _stub_unit(work_unit_id: str = "wu_inline") -> SimpleNamespace:
    return SimpleNamespace(
        work_unit_id=work_unit_id,
        root_workflow_id=f"work-unit:{work_unit_id}",
        design_doc_revision_id="ddr_stub",
        compiled_plan_revision_id="cpr_stub",
        compiled_plan_hash="hash_stub",
        lifecycle_profile_version=1,
        status="RUNNING",
    )


@pytest.fixture
def unit(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    stub = _stub_unit()
    monkeypatch.setattr(root_workflow.repo, "get_work_unit", lambda _wu: stub)
    monkeypatch.setattr(root_workflow.repo, "execution_epoch", lambda _wu: 2)
    return stub


def test_an_unlaunchable_runtime_refuses_the_inline_run_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, unit: SimpleNamespace
) -> None:
    """The live failure: configured DBOS, launch fails, no system-database touch."""

    launch_calls: list[bool] = []
    monkeypatch.setattr(root_workflow, "_dbos_configured", lambda: True)
    monkeypatch.setattr(dbos_app, "launch_dbos", lambda: launch_calls.append(True))
    monkeypatch.setattr(dbos_app, "is_dbos_active", lambda: False)

    def _must_not_run(*_args: Any) -> dict[str, Any]:
        raise AssertionError("a refused inline run must not call the decorated workflow")

    monkeypatch.setattr(root_workflow, "execute_work_unit", _must_not_run)

    out = root_workflow.resume_root_workflow(unit.work_unit_id, EnqueueDelivery.INLINE)

    assert launch_calls, "the inline path must try to launch before giving up"
    assert out["delivered"] is False
    assert "could not be launched" in out["reason"]


def test_a_launchable_runtime_drives_the_inline_run_under_the_continuation_id(
    monkeypatch: pytest.MonkeyPatch, unit: SimpleNamespace
) -> None:
    launched: list[bool] = []
    entered_ids: list[str] = []
    ran_with: list[tuple[Any, ...]] = []

    class _RecordingWorkflowID:
        def __init__(self, workflow_id: str) -> None:
            self._workflow_id = workflow_id

        def __enter__(self) -> "_RecordingWorkflowID":
            entered_ids.append(self._workflow_id)
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    def _run(*args: Any) -> dict[str, Any]:
        ran_with.append(args)
        return {"ok": True}

    monkeypatch.setattr(root_workflow, "_dbos_configured", lambda: True)
    monkeypatch.setattr(dbos_app, "launch_dbos", lambda: launched.append(True))
    monkeypatch.setattr(dbos_app, "is_dbos_active", lambda: bool(launched))
    monkeypatch.setattr(root_workflow, "DBOS", object())
    monkeypatch.setattr(root_workflow, "SetWorkflowID", _RecordingWorkflowID)
    monkeypatch.setattr(root_workflow, "execute_work_unit", _run)

    out = root_workflow.resume_root_workflow(unit.work_unit_id, EnqueueDelivery.INLINE)

    assert entered_ids == [execution_workflow_id(unit.root_workflow_id, 2)]
    assert ran_with == [
        (
            unit.work_unit_id,
            unit.design_doc_revision_id,
            unit.compiled_plan_revision_id,
            unit.compiled_plan_hash,
            unit.lifecycle_profile_version,
        )
    ]
    assert out["delivered"] is True
    assert out["durable"] is True
    assert out["result"] == {"ok": True}


def test_the_identity_decorator_world_still_runs_inline_plainly(
    monkeypatch: pytest.MonkeyPatch, unit: SimpleNamespace
) -> None:
    monkeypatch.setattr(root_workflow, "_dbos_configured", lambda: False)
    monkeypatch.setattr(dbos_app, "is_dbos_active", lambda: False)
    monkeypatch.setattr(root_workflow, "execute_work_unit", lambda *_a: {"ok": "plain"})

    out = root_workflow.start_root_workflow(unit.work_unit_id, EnqueueDelivery.INLINE)

    assert out["delivered"] is True
    assert out["durable"] is False
    assert out["result"] == {"ok": "plain"}


def test_a_durable_request_without_a_runtime_still_stays_pending(
    monkeypatch: pytest.MonkeyPatch, unit: SimpleNamespace
) -> None:
    """The DURABLE refusal predates this fix and must survive it."""

    monkeypatch.setattr(dbos_app, "is_dbos_active", lambda: False)

    out = root_workflow.start_root_workflow(unit.work_unit_id, EnqueueDelivery.DURABLE)

    assert out["delivered"] is False
    assert out["reason"] == "no active DBOS runtime; the enqueue stays pending"
