# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""DBOS boundaries for the coordination ledger command surface.

The coordination ledger remains normal database tables accessed through
``agent_coordination_mcp.py``. DBOS wraps semantic command/lease boundaries so a
runtime can retry or resume around those rows without hiding state inside DBOS
workflow internals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .._dbos_runtime import DBOS, SetWorkflowID, dbos_step, dbos_workflow
from ..constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
from ..ids import sha256_text
from ..pow_wow.ledger import run_coordination_command
from ..settings import Settings
from .contracts import OpenExecutionLease, RawCoordinationCommand


class CoordinationCommandRequest(BaseModel):
    args: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=30, gt=0)
    coordination_root: str | None = None
    backend: Literal["postgres"] = "postgres"
    database_url: str | None = None


class ExternalAgentLeaseBoundaryRequest(BaseModel):
    idempotency_key: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    timeout_seconds: int = Field(default=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS, gt=0)
    intent_id: str | None = None
    task_id: str | None = None
    agent_tier: str | None = None
    agent_name: str | None = None
    worktree_path: str | None = None
    command: list[str] = Field(default_factory=list)
    compensation: dict[str, Any] = Field(default_factory=dict)
    coordination_root: str | None = None
    backend: Literal["postgres"] = "postgres"
    database_url: str | None = None


def _settings_for_coordination(
    *,
    backend: Literal["postgres"],
    database_url: str | None,
    coordination_root: str | None,
) -> Settings:
    # An omitted key is not the same as an explicit None: only the former lets
    # Settings read its environment alias. Passing None through pinned the ledger
    # URL to nothing and left the app database as the only candidate, which on a
    # deployment where those two differ is the wrong database.
    data: dict[str, Any] = {"coordination_backend": backend}
    if coordination_root is not None:
        data["coordination_root"] = coordination_root
    if database_url is not None:
        data["coordination_database_url"] = database_url
        data["database_url"] = database_url
    return Settings.model_validate(data)


def _root_path(raw: str | None) -> Path | None:
    if raw is None:
        return None
    return Path(raw).expanduser()


@dbos_step()
def run_coordination_command_step(payload: dict[str, Any]) -> dict[str, Any]:
    request = CoordinationCommandRequest.model_validate(payload)
    settings = _settings_for_coordination(
        backend=request.backend,
        database_url=request.database_url,
        coordination_root=request.coordination_root,
    )
    return run_coordination_command(
        RawCoordinationCommand.from_argv(request.args),
        timeout=request.timeout_seconds,
        settings=settings,
        root=_root_path(request.coordination_root),
    )


@dbos_workflow()
def coordination_command_workflow(payload: dict[str, Any]) -> dict[str, Any]:
    return run_coordination_command_step(payload)


def run_coordination_command_durably(
    request: CoordinationCommandRequest,
    *,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    payload = request.model_dump(mode="json")
    if DBOS is not None and SetWorkflowID is not None:
        from ..dbos_app import is_dbos_active, launch_dbos

        launch_dbos()
        if is_dbos_active():
            durable_id = (
                workflow_id or f"coordination_command:{sha256_text(request.model_dump_json())}"
            )
            with SetWorkflowID(durable_id):
                return coordination_command_workflow(payload)
    return run_coordination_command_step(payload)


@dbos_step()
def open_external_agent_execution_lease_step(payload: dict[str, Any]) -> dict[str, Any]:
    request = ExternalAgentLeaseBoundaryRequest.model_validate(payload)
    settings = _settings_for_coordination(
        backend=request.backend,
        database_url=request.database_url,
        coordination_root=request.coordination_root,
    )
    return run_coordination_command(
        OpenExecutionLease(
            idempotency_key=request.idempotency_key,
            worker_id=request.worker_id,
            timeout_seconds=request.timeout_seconds,
            intent_id=request.intent_id,
            task_id=request.task_id,
            agent_tier=request.agent_tier,
            agent_name=request.agent_name,
            worktree_path=request.worktree_path,
            command=tuple(request.command),
            compensation=request.compensation or None,
        ),
        timeout=30,
        settings=settings,
        root=_root_path(request.coordination_root),
    )


def _external_agent_execution_lease_boundary(payload: dict[str, Any]) -> dict[str, Any]:
    """Open the durable row that an external CLI worker must heartbeat/complete.

    The workflow does not run arbitrary agent code. It establishes the retry and
    cancel boundary; a runner then uses the returned lease ID while executing in
    an isolated worktree.
    """

    opened = open_external_agent_execution_lease_step(payload)
    return {
        "ok": True,
        "schema_version": "external_agent_execution_lease_boundary.v1",
        "lease_open_result": opened,
        "next_action": "execute_external_agent_and_complete_lease",
    }


@dbos_workflow()
def external_agent_execution_lease_workflow(payload: dict[str, Any]) -> dict[str, Any]:
    return _external_agent_execution_lease_boundary(payload)


def open_external_agent_execution_lease_durably(
    request: ExternalAgentLeaseBoundaryRequest,
    *,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    payload = request.model_dump(mode="json")
    if DBOS is not None and SetWorkflowID is not None:
        from ..dbos_app import is_dbos_active, launch_dbos

        launch_dbos()
        if is_dbos_active():
            durable_id = workflow_id or f"external_agent_lease:{request.idempotency_key}"
            with SetWorkflowID(durable_id):
                return external_agent_execution_lease_workflow(payload)
    return _external_agent_execution_lease_boundary(payload)


__all__ = [
    "CoordinationCommandRequest",
    "ExternalAgentLeaseBoundaryRequest",
    "coordination_command_workflow",
    "external_agent_execution_lease_workflow",
    "open_external_agent_execution_lease_durably",
    "open_external_agent_execution_lease_step",
    "run_coordination_command_durably",
    "run_coordination_command_step",
]
