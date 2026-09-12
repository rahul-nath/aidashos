# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Resolve a produced plan through its retained task, never an executor summary."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from ..coordination.store import rowdict, tx
from ..ids import sha256_text
from ..verification_git import verification_git_environment

PLAN_REPORT_INSTRUCTION = """Final implementation-plan report contract: plan_result.v1.
Return only one JSON object, without a Markdown fence or surrounding commentary.
If you produced the plan, return:
{"schema_version":"plan_result.v1","status":"PLANNED","plan_markdown":"<the actual plan>"}
The plan includes source findings, changes, verification, and remaining uncertainty.
If you cannot produce the plan, return:
{"schema_version":"plan_result.v1","status":"UNAVAILABLE","reason":"<what prevented planning>"}
A completion summary, refusal, or proposed future investigation is not a produced plan.
"""


class _Report(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)
    schema_version: Literal["plan_result.v1"]


class ProducedPlan(_Report):
    status: Literal["PLANNED"]
    plan_markdown: str = Field(min_length=1)

    @field_validator("plan_markdown")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("plan must contain text")
        return value


class UnavailablePlan(_Report):
    status: Literal["UNAVAILABLE"]
    reason: str = Field(min_length=1)


_PLAN_REPORT = TypeAdapter(Annotated[ProducedPlan | UnavailablePlan, Field(discriminator="status")])
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_RUN_SCHEMAS = {"cli_agent_run": "cli_agent_run.v1", "delegated_task_run": "delegated_task_run.v2"}


class PlanEvidenceCause(StrEnum):
    HOST_EVIDENCE_UNAVAILABLE = "plan_host_evidence_unavailable"
    REPORT_INVALID = "plan_report_invalid"
    REPORT_UNAVAILABLE = "plan_report_unavailable"


class PlanEvidenceUnavailable(ValueError):
    """No exact produced report could be credited to this plan milestone."""

    def __init__(
        self, reason: str, cause: PlanEvidenceCause = PlanEvidenceCause.HOST_EVIDENCE_UNAVAILABLE
    ) -> None:
        self.cause = cause
        super().__init__(reason)


def _object(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanEvidenceUnavailable("plan evidence has an invalid object")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanEvidenceUnavailable("plan evidence is missing a required identity or report")
    return value


def _retained_json(content: str) -> Any:
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise PlanEvidenceUnavailable("retained plan evidence is not valid JSON") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlanEvidenceUnavailable(
                "plan report has duplicate JSON fields", PlanEvidenceCause.REPORT_INVALID
            )
        result[key] = value
    return result


def parse_plan_report(output: object) -> ProducedPlan:
    if not isinstance(output, str):
        raise PlanEvidenceUnavailable(
            "final task report is not text", PlanEvidenceCause.REPORT_INVALID
        )
    try:
        report = _PLAN_REPORT.validate_python(json.loads(output, object_pairs_hook=_unique_object))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise PlanEvidenceUnavailable(
            "final task did not produce a valid plan_result.v1 report",
            PlanEvidenceCause.REPORT_INVALID,
        ) from exc
    if isinstance(report, UnavailablePlan):
        raise PlanEvidenceUnavailable(
            f"final task could not produce its plan: {report.reason}",
            PlanEvidenceCause.REPORT_UNAVAILABLE,
        )
    return report


def observe_local_plan_source(repository: Path) -> str | None:
    """Stamp the local delegate's source before invocation without caller Git redirects.

    General delegation can target a non-Git project. Such a run remains usable as
    advice but cannot satisfy source-bound implementation-plan evidence.
    """

    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--verify", "HEAD"],
            env=verification_git_environment(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and _SOURCE_SHA.fullmatch(revision) else None


def resolve_implementation_plan(
    *,
    intent_id: str,
    target_project_id: str,
    dispatch_payload: Mapping[str, Any],
    run_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Retain one terminal task's report and exact persisted provenance.

    The recorded dependency graph selects the final answer, including an all-local
    graph. A renamed task or another agent's intermediate answer cannot replace it.
    Dispatch observations must equal the separately retained task artifact; neither
    role names nor the runner's summary grant evidence authority.
    """

    pow_wow_id = _text(dispatch_payload.get("pow_wow_id"))
    task_ids = _object(dispatch_payload.get("task_ids_by_name"))
    if run_result.get("pow_wow_id") != pow_wow_id:
        raise PlanEvidenceUnavailable("plan dispatch and task run disagree on pow-wow identity")
    with tx() as connection:
        tasks = [
            rowdict(row)
            for row in connection.execute(
                "SELECT task_id, task_name, status, blocked_by_json "
                "FROM saga_tasks WHERE pow_wow_id = ?",
                (pow_wow_id,),
            ).fetchall()
        ]
        if (
            not tasks
            or len(tasks) != len(task_ids)
            or {task["task_name"]: task["task_id"] for task in tasks} != dict(task_ids)
        ):
            raise PlanEvidenceUnavailable("plan dispatch does not match its retained task graph")
        dependencies: set[str] = set()
        for task in tasks:
            blocked_by = _retained_json(task["blocked_by_json"])
            if not isinstance(blocked_by, list) or any(
                not isinstance(name, str) or name not in task_ids for name in blocked_by
            ):
                raise PlanEvidenceUnavailable("plan task dependencies are invalid")
            dependencies.update(blocked_by)
        final_tasks = [task for task in tasks if task["task_name"] not in dependencies]
        if len(final_tasks) != 1 or any(task["status"] != "COMPLETED" for task in tasks):
            raise PlanEvidenceUnavailable(
                "plan needs one completed terminal task and completed dependencies"
            )
        task = final_tasks[0]
        observations = [
            item
            for item in run_result.get("tasks", ())
            if _object(item).get("task_name") == task["task_name"]
        ]
        if len(observations) != 1 or observations[0].get("status") != "completed":
            raise PlanEvidenceUnavailable("final plan task has no completed dispatch observation")
        captures = [
            item
            for item in observations[0].get("artifacts", ())
            if _object(item).get("artifact_type") in _RUN_SCHEMAS
        ]
        if len(captures) != 1:
            raise PlanEvidenceUnavailable(
                "final plan task needs exactly one supported produced report"
            )
        capture = _object(captures[0])
        schema = _RUN_SCHEMAS[_text(capture.get("artifact_type"))]
        content = _object(capture.get("content"))
        if capture.get("schema_version") != schema or content.get("schema_version") != schema:
            raise PlanEvidenceUnavailable("final plan capture schema is not supported")
        if content.get("target_project_id") != target_project_id:
            raise PlanEvidenceUnavailable("final plan capture targets another project")
        captured_task = _object(content.get("task"))
        if (
            captured_task.get("task_name") != task["task_name"]
            or captured_task.get("purpose") != "advisory"
        ):
            raise PlanEvidenceUnavailable("final plan capture does not describe this advisory task")
        retained = [
            rowdict(row)
            for row in connection.execute(
                "SELECT artifact_id, content FROM task_artifacts "
                "WHERE task_id = ? AND pow_wow_id = ? AND artifact_type = ? AND schema_version = ?",
                (task["task_id"], pow_wow_id, capture["artifact_type"], schema),
            ).fetchall()
        ]
        matching = [
            row
            for row in retained
            if _object(_retained_json(row["content"])).get("content") == content
        ]
        if len(matching) != 1:
            raise PlanEvidenceUnavailable(
                "final plan report does not match one retained task artifact"
            )
        if schema == "cli_agent_run.v1":
            attempt = _object(content.get("execution_lease"))
            lease_id = _text(attempt.get("lease_id"))
            lease = connection.execute(
                "SELECT source_revision FROM agent_execution_leases "
                "WHERE lease_id = ? AND intent_id = ? AND task_id = ? "
                "AND target_project_id = ? AND status = 'COMPLETED'",
                (lease_id, intent_id, task["task_id"], target_project_id),
            ).fetchone()
            if lease is None or attempt.get("task_id") != task["task_id"]:
                raise PlanEvidenceUnavailable(
                    "final plan has no completed lease for this dispatch task"
                )
            source_revision = rowdict(lease)["source_revision"]
            origin = {"kind": "FRONTIER_EXECUTION", "lease_id": lease_id}
        else:
            subject = _object(content.get("execution_subject"))
            provenance = _object(content.get("provenance"))
            if (
                subject.get("intent_id") != intent_id
                or subject.get("task_id") != task["task_id"]
                or content.get("ok") is not True
            ):
                raise PlanEvidenceUnavailable("local plan capture belongs to another dispatch task")
            source_revision = subject.get("source_revision")
            origin = {
                "kind": "LOCAL_MODEL_INVOCATION",
                "invocation_id": _text(provenance.get("invocation_id")),
                "output_artifact_id": _text(provenance.get("output_artifact_id")),
            }
        if not isinstance(source_revision, str) or not _SOURCE_SHA.fullmatch(source_revision):
            raise PlanEvidenceUnavailable("final plan has no immutable source revision")
        output = content.get("output")
        report = parse_plan_report(output)
        assert isinstance(output, str)  # parse_plan_report established the produced report type.
    return {
        "schema_version": "implementation_plan_evidence.v1",
        "report": report.model_dump(mode="json"),
        "report_sha256": sha256_text(output),
        "plan_sha256": sha256_text(report.plan_markdown),
        "dispatch_intent_id": intent_id,
        "pow_wow_id": pow_wow_id,
        "task_id": task["task_id"],
        "task_name": task["task_name"],
        "target_project_id": target_project_id,
        "source_revision": source_revision,
        "source_artifact_id": matching[0]["artifact_id"],
        "origin": origin,
    }
