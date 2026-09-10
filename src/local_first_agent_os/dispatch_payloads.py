# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Strict report ingress; reported artifacts are observations, never credentials."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from .coordination.contracts import DispatchKind
from .coordination.failures import FailureV1
from .coordination.outcomes import DispatchPromotionState, DispatchResultOrigin, DispatchResultState
from .pow_wow.types import PowWowRunStatus, PowWowTaskStatus


class _StrictReport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)


class UnverifiedArtifactObservation(_StrictReport):
    """A typed container for caller claims; its content carries no trusted authority.

    Artifact-specific owners must decode their registered content schema and
    resolve host-owned provenance before admitting a transition from it.
    """

    artifact_type: str
    content: dict[str, JsonValue]
    task_name: str | None = None
    schema_version: str = Field(default="pow_wow_artifact.v1", min_length=1)
    persisted_artifact_id: str | None = None


class TaskObservation(_StrictReport):
    task_name: str = ""
    role: str = ""
    status: PowWowTaskStatus | Literal["APPROVE", "COMPLETED"] | None = None
    summary: str = ""
    changed_files: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()
    verification_output: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    artifacts: tuple[UnverifiedArtifactObservation, ...] = ()
    failure: FailureV1 | None = None

    @model_validator(mode="after")
    def legacy_host_status_is_scoped(self) -> TaskObservation:
        if self.status == "COMPLETED" and self.task_name != "review_verdict_recovery":
            raise ValueError("legacy uppercase task status belongs only to host parser recovery")
        return self


class _RunObservation(_StrictReport):
    executor: str | None = None
    mode: str | None = None
    pow_wow_id: str | None = None
    target_project_id: str | None = None
    target_project_path: str | None = None
    output_summary: str = ""
    tasks: tuple[TaskObservation, ...] = ()
    changed_files: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()
    verification_output: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    artifacts: tuple[UnverifiedArtifactObservation, ...] = ()
    external_agents_started: bool | None = None
    auto_merge: bool | None = None
    traceback: str | None = None


class AutomatedRunObservation(_RunObservation):
    status: PowWowRunStatus


class ManualRunObservation(_RunObservation):
    status: Literal["MANUAL_RECOVERY_REVIEWED"]


class ManualRecoveryReview(_StrictReport):
    verdict: Literal["APPROVE"]
    initial_verdict: str = ""
    initial_finding: str = ""
    resolution: str = ""
    risks: tuple[str, ...] = ()


class ManualRecoveryEvidence(_StrictReport):
    """The registered evidence projection of a legacy approval envelope."""

    purpose: str = ""
    target_project_id: str | None = None
    branch: str = Field(min_length=1)
    base_sha: str = Field(min_length=1)
    commit_sha: str = Field(min_length=1)
    changed_files: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    staff_review: ManualRecoveryReview


class InterruptedRunObservation(_RunObservation):
    """Declared infrastructure outcomes retain evidence without granting success."""

    status: Literal["TIMED_OUT", "CANCELED", "UNAVAILABLE"]


type ValidatedRunObservation = (
    AutomatedRunObservation | ManualRunObservation | InterruptedRunObservation
)


RunObservation = Annotated[ValidatedRunObservation, Field(discriminator="status")]
RUN_OBSERVATION = TypeAdapter(RunObservation)


class DispatchReportEnvelope(_StrictReport):
    schema_version: Literal["dispatch_runner_result.v1"]
    run_result: RunObservation
    result_origin: DispatchResultOrigin | None = None
    result_state: DispatchResultState | None = None
    promotion_state: DispatchPromotionState | None = None
    intent_id: str | None = None
    target_project_id: str | None = None
    tier: Literal["junior", "senior", "staff"] | None = None
    kind: DispatchKind | None = None
    saga_id: str | None = None
    pow_wow_id: str | None = None
    task_id: str | None = None
    task_ids_by_name: dict[str, str] | None = None
    # These are retained diagnostic records, not authority to execute or merge.
    decomposition: dict[str, JsonValue] | None = None
    merge_approval: dict[str, JsonValue] | None = None
    pairing_assignment: dict[str, JsonValue] | None = None
    interrupted_recovery: dict[str, JsonValue] | None = None
    recovered_from_intent_id: str | None = None
