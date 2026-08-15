# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..coordination.contracts import (
    CompleteTask,
    CoordinationCommand,
    CoordinationResult,
    FailTask,
    SubmitArtifact,
    parse_coordination_result,
)
from ..coordination.transport import (
    CoordinationTransportFactory,
    command_from_argv,
    coordination_database_url,
    coordination_root,
)
from ..settings import Settings
from .types import PowWowArtifact, PowWowRunResult


def describe_coordination_ledger(settings: Settings | None = None) -> str:
    """Name the ledger an operator is actually looking at, without its password.

    A URL in an operator summary is a string someone will paste into a message, so
    the credentials come out before it is ever rendered.
    """

    url = coordination_database_url(settings)
    if not url:
        return "postgres (no database url configured)"
    redacted = url
    if "@" in url:
        scheme, _, rest = url.partition("://")
        credentials, _, host = rest.partition("@")
        user, _, _password = credentials.partition(":")
        redacted = f"{scheme}://{user}:***@{host}"
    schema = os.environ.get("AGENT_COORDINATION_SCHEMA")
    return f"{redacted}#{schema}" if schema else redacted


def resolve_coordination_events_path(
    settings: Settings | None = None, root: Path | None = None
) -> Path:
    return coordination_root(settings, root) / ".agent_coordination" / "events.jsonl"


def run_coordination_command(
    args: list[str] | CoordinationCommand,
    *,
    timeout: int = 30,
    settings: Settings | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    command = command_from_argv(args) if isinstance(args, list) else args
    transport = CoordinationTransportFactory.create(
        settings=settings,
        root=root,
        timeout_seconds=timeout,
    )
    return dict(transport.execute(command))


def run_typed_coordination_command(
    command: CoordinationCommand,
    *,
    timeout: int = 30,
    settings: Settings | None = None,
    root: Path | None = None,
) -> CoordinationResult:
    payload = run_coordination_command(
        command,
        timeout=timeout,
        settings=settings,
        root=root,
    )
    return parse_coordination_result(command, payload)


def serialize_coordination_content_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def _build_review_reference_key(artifact: PowWowArtifact) -> tuple[str, str] | None:
    if artifact.artifact_type != "review_result" or artifact.schema_version != "review_result.v1":
        return None
    review_text = artifact.content.get("review_text")
    if not isinstance(review_text, str) or not review_text:
        return None
    return artifact.task_name or "", hashlib.sha256(review_text.encode("utf-8")).hexdigest()


def _build_artifact_persistence_payload(
    artifact: PowWowArtifact,
    review_artifact_ids: Mapping[tuple[str, str], str],
) -> dict[str, Any]:
    payload = artifact.to_payload()
    if artifact.schema_version != "bounded_revision_context.v1":
        return payload
    content = payload.get("content")
    reviewer_output = content.get("reviewer_output") if isinstance(content, dict) else None
    if not isinstance(reviewer_output, dict):
        raise RuntimeError("bounded revision context is missing reviewer_output")
    if reviewer_output.get("state") == "DURABLE_ARTIFACT":
        if not reviewer_output.get("artifact_id"):
            raise RuntimeError("durable reviewer output reference has no artifact_id")
        return payload
    key = (
        str(reviewer_output.get("task_name") or ""),
        str(reviewer_output.get("review_text_sha256") or ""),
    )
    artifact_id = review_artifact_ids.get(key)
    if not artifact_id:
        raise RuntimeError("bounded revision context cannot resolve its exact review_result.v1")
    reviewer_output["state"] = "DURABLE_ARTIFACT"
    reviewer_output["artifact_id"] = artifact_id
    return payload


def persist_pow_wow_run_result(
    pow_wow_id: str,
    task_ids_by_name: Mapping[str, str],
    run_result: PowWowRunResult,
    *,
    complete_completed_tasks: bool = True,
    record_failed_tasks: bool = True,
    timeout: int = 15,
    settings: Settings | None = None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    review_artifact_ids: dict[tuple[str, str], str] = {}
    for task_result in run_result.tasks:
        task_id = task_ids_by_name.get(task_result.task_name)
        for artifact in task_result.artifacts:
            review_key = _build_review_reference_key(artifact)
            if artifact.persisted_artifact_id is not None:
                if review_key is not None:
                    review_artifact_ids[review_key] = artifact.persisted_artifact_id
                continue
            persisted = run_coordination_command(
                SubmitArtifact(
                    pow_wow_id=pow_wow_id,
                    artifact_type=artifact.artifact_type,
                    content=serialize_coordination_content_to_json(
                        _build_artifact_persistence_payload(artifact, review_artifact_ids)
                    ),
                    schema_version=artifact.schema_version,
                    task_id=task_id,
                ),
                timeout=timeout,
                settings=settings,
                root=root,
            )
            events.append(persisted)
            if review_key is not None:
                artifact_id = persisted.get("artifact_id")
                if not isinstance(artifact_id, str) or not artifact_id:
                    raise RuntimeError("review_result.v1 persistence returned no artifact_id")
                review_artifact_ids[review_key] = artifact_id
        if task_id and complete_completed_tasks and task_result.status == "completed":
            events.append(
                run_coordination_command(
                    CompleteTask(task_id),
                    timeout=timeout,
                    settings=settings,
                    root=root,
                )
            )
        elif task_id and record_failed_tasks and task_result.status in ("failed", "blocked"):
            events.append(
                run_coordination_command(
                    FailTask(task_id, f"{task_result.status}: {task_result.summary}"),
                    timeout=timeout,
                    settings=settings,
                    root=root,
                )
            )

    for artifact in run_result.artifacts:
        events.append(
            run_coordination_command(
                SubmitArtifact(
                    pow_wow_id=pow_wow_id,
                    artifact_type=artifact.artifact_type,
                    content=serialize_coordination_content_to_json(artifact.to_payload()),
                    schema_version=artifact.schema_version,
                ),
                timeout=timeout,
                settings=settings,
                root=root,
            )
        )

    events.append(
        run_coordination_command(
            SubmitArtifact(
                pow_wow_id=pow_wow_id,
                artifact_type="pow_wow_dispatch_summary",
                content=serialize_coordination_content_to_json(run_result.to_payload()),
                schema_version="pow_wow_run_result.v1",
            ),
            timeout=timeout,
            settings=settings,
            root=root,
        )
    )
    return events
