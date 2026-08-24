# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The typed operator action that asks the refinery to drain an approval."""

from __future__ import annotations

from typing import Annotated, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field

from ..contracts import ApprovalRequestType, ApprovalStatus
from ..coordination.integration_queue import read_integration_requests
from ..coordination.store import connect
from .requests import BisectedOut, InFlight, Integrated, Queued, Withdrawn


class _TriggerResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_serialization_defaults_required=True,
    )

    approval_id: str


class _IdentifiedTriggerResult(_TriggerResult):
    request_id: str
    target_project_id: str


class IntegrationAccepted(_IdentifiedTriggerResult):
    state: Literal["accepted"] = "accepted"
    message: str = "The approved request was handed to the refinery."


class IntegrationRunning(_IdentifiedTriggerResult):
    state: Literal["running"] = "running"
    message: str = "The refinery is already integrating this request."


class IntegrationComplete(_IdentifiedTriggerResult):
    state: Literal["complete"] = "complete"
    message: str = "The approved request is already integrated."


class IntegrationBlocked(_TriggerResult):
    request_id: str | None
    target_project_id: str | None
    state: Literal["blocked"] = "blocked"
    message: str


IntegrationTriggerResult = Annotated[
    IntegrationAccepted | IntegrationRunning | IntegrationComplete | IntegrationBlocked,
    Field(discriminator="state"),
]


def plan_integration_trigger(approval_id: str) -> IntegrationTriggerResult:
    """Read durable state once and decide whether a click may start the refinery."""

    with connect() as connection:
        approval = connection.execute(
            "SELECT request_type, status FROM approval_requests WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        requests = read_integration_requests(connection, approval_id=approval_id)

    if approval is None:
        return IntegrationBlocked(
            approval_id=approval_id,
            request_id=None,
            target_project_id=None,
            message="The approval request does not exist.",
        )
    if str(approval["request_type"]) != ApprovalRequestType.CODE_MERGE.value:
        return IntegrationBlocked(
            approval_id=approval_id,
            request_id=None,
            target_project_id=None,
            message="Only a CODE_MERGE approval can start integration.",
        )
    status = ApprovalStatus(str(approval["status"]))
    if status is not ApprovalStatus.APPROVED:
        return IntegrationBlocked(
            approval_id=approval_id,
            request_id=None,
            target_project_id=None,
            message=f"The CODE_MERGE approval is {status.value}, not APPROVED.",
        )
    if len(requests) != 1:
        return IntegrationBlocked(
            approval_id=approval_id,
            request_id=None,
            target_project_id=None,
            message=(
                f"Expected one integration request for approval {approval_id}, "
                f"found {len(requests)}."
            ),
        )

    request = requests[0]
    request_id = str(request.subject.request_id)
    target_project_id = request.subject.target_project_id
    match request:
        case Queued():
            return IntegrationAccepted(
                approval_id=approval_id,
                request_id=request_id,
                target_project_id=target_project_id,
            )
        case InFlight():
            return IntegrationRunning(
                approval_id=approval_id,
                request_id=request_id,
                target_project_id=target_project_id,
            )
        case Integrated():
            return IntegrationComplete(
                approval_id=approval_id,
                request_id=request_id,
                target_project_id=target_project_id,
            )
        case BisectedOut():
            return IntegrationBlocked(
                approval_id=approval_id,
                request_id=request_id,
                target_project_id=target_project_id,
                message="The request was parked after a merge conflict or red project gate.",
            )
        case Withdrawn():
            return IntegrationBlocked(
                approval_id=approval_id,
                request_id=request_id,
                target_project_id=target_project_id,
                message=f"The request was withdrawn: {request.reason.value}.",
            )
    assert_never(request)


__all__ = [
    "IntegrationAccepted",
    "IntegrationBlocked",
    "IntegrationComplete",
    "IntegrationRunning",
    "IntegrationTriggerResult",
    "plan_integration_trigger",
]
