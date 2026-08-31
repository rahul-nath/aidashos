# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections.abc import Callable
from typing import cast
from unittest.mock import Mock

from local_first_agent_os import operator_commands
from local_first_agent_os.operator_commands import (
    CancelWorkUnit,
    IntegrationTriggered,
    OperatorExecutionContext,
    TriggerIntegration,
    WorkUnitCancelled,
    execute_operator_command,
)
from local_first_agent_os.operator_identity import verify_operator_actor
from local_first_agent_os.refinery.trigger import (
    IntegrationAccepted,
    IntegrationTriggerResult,
    plan_integration_trigger,
)
from local_first_agent_os.settings import Settings
from local_first_agent_os.work_units.commands import cancel_work_unit


def _context(
    submit_integration: Callable[[str], None] | None = None,
    plan_integration: Callable[[str], IntegrationTriggerResult] | None = None,
) -> OperatorExecutionContext:
    return OperatorExecutionContext(
        settings=cast(Settings, Mock(spec=Settings)),
        submit_integration=submit_integration,
        plan_integration=plan_integration or plan_integration_trigger,
    )


def test_cancel_command_uses_the_application_service(monkeypatch) -> None:
    cancel = Mock(return_value={"work_unit_id": "wu_1", "status": "CANCELLED"})
    monkeypatch.setattr(operator_commands.work_units, "cancel_work_unit", cancel)

    result = execute_operator_command(
        CancelWorkUnit("wu_1", "operator requested", verify_operator_actor("operator")),
        context=_context(),
    )

    assert result == WorkUnitCancelled({"work_unit_id": "wu_1", "status": "CANCELLED"})
    cancel.assert_called_once_with("wu_1", reason="operator requested")


def test_integration_command_owns_submission_after_planning(monkeypatch) -> None:
    accepted = IntegrationAccepted(
        approval_id="apr_1",
        request_id="int_1",
        target_project_id="local_first_agent_os",
    )
    plan = Mock(return_value=accepted)
    submit = Mock()

    result = execute_operator_command(
        TriggerIntegration("apr_1", verify_operator_actor("operator")),
        context=_context(submit_integration=submit, plan_integration=plan),
    )

    assert result == IntegrationTriggered(accepted)
    plan.assert_called_once_with("apr_1")
    submit.assert_called_once_with("local_first_agent_os")


def test_privileged_work_unit_command_refuses_without_operator_proof(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_AGENT_OPERATOR_TOKEN")

    refused = cancel_work_unit("wu_1")

    assert refused["ok"] is False
    assert refused["error"] == "operator_token_required"
