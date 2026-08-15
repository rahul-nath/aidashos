# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The trusted milestone executor registry.

A compiled plan selects an executor by name from this registry and can do nothing
else. It cannot supply code, widen a tool set, lengthen a timeout, or turn an
approval off. That is the whole point of the registry: document authority stops
at choosing among behaviors the runtime already trusts.

Each declaration states the phase it belongs to, the schemas it speaks, the tools
it may use, and the evidence it must produce. The compiler rejects a milestone
whose executor is unregistered or declared for another phase.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ..capabilities import Capability
from .events import ArtifactKind
from .lifecycle import LifecyclePhase


class ExecutorKind(StrEnum):
    """Every executor a DesignDoc may name."""

    CLARIFY_REQUIREMENTS = "clarify.requirements"
    VALIDATE_REPOSITORY = "validate.repository"
    PLAN_IMPLEMENTATION = "plan.implementation"
    IMPLEMENT_CODE_CHANGE = "implement.code_change"
    VERIFY_TESTS = "verify.tests"
    VERIFY_ACCEPTANCE = "verify.acceptance"
    REVIEW_AGENT = "review.agent"
    REVIEW_OPERATOR = "review.operator"
    DELIVER_ARTIFACT = "deliver.artifact"
    DELIVER_DEPLOYMENT = "deliver.deployment"


class ApprovalRequirement(StrEnum):
    """Whether operator approval is optional, forced on, or forbidden.

    A boolean could not express "the document may ask for approval here" and
    "approval is not the document's decision here" at the same time, which is
    exactly the distinction a deployment executor needs.
    """

    OPTIONAL = "OPTIONAL"
    ALWAYS = "ALWAYS"
    NEVER = "NEVER"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff_seconds: float

    def to_payload(self) -> dict[str, float | int]:
        return {"max_attempts": self.max_attempts, "backoff_seconds": self.backoff_seconds}


@dataclass(frozen=True)
class ExecutorDeclaration:
    """What one executor kind is allowed to do, and what it owes back."""

    kind: ExecutorKind
    phase: LifecyclePhase
    input_schema: str
    output_schema: str
    permitted_tools: tuple[Capability, ...]
    retry: RetryPolicy
    timeout_seconds: int
    required_artifact_types: tuple[ArtifactKind, ...]
    approval: ApprovalRequirement

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "phase": self.phase.value,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "permitted_tools": [item.value for item in self.permitted_tools],
            "retry": self.retry.to_payload(),
            "timeout_seconds": self.timeout_seconds,
            "required_artifact_types": [item.value for item in self.required_artifact_types],
            "approval": self.approval.value,
        }


_BOUNDED_RETRY: Final = RetryPolicy(max_attempts=3, backoff_seconds=2.0)
_SINGLE_ATTEMPT: Final = RetryPolicy(max_attempts=1, backoff_seconds=0.0)


_DECLARATIONS: Final[tuple[ExecutorDeclaration, ...]] = (
    ExecutorDeclaration(
        kind=ExecutorKind.CLARIFY_REQUIREMENTS,
        phase=LifecyclePhase.CLARIFY,
        input_schema="clarify_request.v1",
        output_schema="clarify_result.v1",
        permitted_tools=(Capability.READ_REPOSITORY, Capability.ASK_OPERATOR),
        retry=_BOUNDED_RETRY,
        timeout_seconds=900,
        required_artifact_types=(ArtifactKind.CLARIFICATION_RECORD,),
        approval=ApprovalRequirement.OPTIONAL,
    ),
    ExecutorDeclaration(
        kind=ExecutorKind.VALIDATE_REPOSITORY,
        phase=LifecyclePhase.VALIDATE,
        input_schema="validate_request.v1",
        output_schema="validate_result.v1",
        permitted_tools=(Capability.READ_REPOSITORY, Capability.RUN_COMMAND),
        retry=_BOUNDED_RETRY,
        timeout_seconds=900,
        required_artifact_types=(ArtifactKind.ENVIRONMENT_REPORT,),
        approval=ApprovalRequirement.NEVER,
    ),
    ExecutorDeclaration(
        kind=ExecutorKind.PLAN_IMPLEMENTATION,
        phase=LifecyclePhase.PLAN,
        input_schema="plan_request.v1",
        output_schema="plan_result.v1",
        permitted_tools=(Capability.READ_REPOSITORY, Capability.INVOKE_MODEL),
        retry=_BOUNDED_RETRY,
        timeout_seconds=1800,
        required_artifact_types=(ArtifactKind.IMPLEMENTATION_PLAN,),
        approval=ApprovalRequirement.OPTIONAL,
    ),
    ExecutorDeclaration(
        kind=ExecutorKind.IMPLEMENT_CODE_CHANGE,
        phase=LifecyclePhase.IMPLEMENT,
        input_schema="implement_request.v1",
        output_schema="implement_result.v1",
        permitted_tools=(
            Capability.READ_REPOSITORY,
            Capability.WRITE_REPOSITORY,
            Capability.INVOKE_MODEL,
            Capability.RUN_COMMAND,
        ),
        retry=_BOUNDED_RETRY,
        timeout_seconds=5400,
        required_artifact_types=(ArtifactKind.SOURCE_PATCH,),
        approval=ApprovalRequirement.OPTIONAL,
    ),
    ExecutorDeclaration(
        kind=ExecutorKind.VERIFY_TESTS,
        phase=LifecyclePhase.VERIFY,
        input_schema="verify_request.v1",
        output_schema="verify_result.v1",
        permitted_tools=(Capability.READ_REPOSITORY, Capability.RUN_COMMAND),
        retry=_BOUNDED_RETRY,
        timeout_seconds=3600,
        required_artifact_types=(ArtifactKind.TEST_RESULT,),
        approval=ApprovalRequirement.NEVER,
    ),
    ExecutorDeclaration(
        kind=ExecutorKind.VERIFY_ACCEPTANCE,
        phase=LifecyclePhase.VERIFY,
        input_schema="verify_request.v1",
        output_schema="verify_result.v1",
        permitted_tools=(
            Capability.READ_REPOSITORY,
            Capability.RUN_COMMAND,
            Capability.INVOKE_MODEL,
        ),
        retry=_BOUNDED_RETRY,
        timeout_seconds=3600,
        required_artifact_types=(ArtifactKind.ACCEPTANCE_REPORT,),
        approval=ApprovalRequirement.NEVER,
    ),
    ExecutorDeclaration(
        kind=ExecutorKind.REVIEW_AGENT,
        phase=LifecyclePhase.REVIEW,
        input_schema="review_request.v1",
        output_schema="review_result.v1",
        permitted_tools=(Capability.READ_REPOSITORY, Capability.INVOKE_MODEL),
        retry=_BOUNDED_RETRY,
        timeout_seconds=1800,
        required_artifact_types=(ArtifactKind.REVIEW_DECISION,),
        approval=ApprovalRequirement.OPTIONAL,
    ),
    ExecutorDeclaration(
        kind=ExecutorKind.REVIEW_OPERATOR,
        phase=LifecyclePhase.REVIEW,
        input_schema="review_request.v1",
        output_schema="review_result.v1",
        permitted_tools=(Capability.READ_REPOSITORY,),
        retry=_SINGLE_ATTEMPT,
        timeout_seconds=86400,
        required_artifact_types=(ArtifactKind.OPERATOR_APPROVAL,),
        approval=ApprovalRequirement.ALWAYS,
    ),
    ExecutorDeclaration(
        kind=ExecutorKind.DELIVER_ARTIFACT,
        phase=LifecyclePhase.DELIVER,
        input_schema="deliver_request.v1",
        output_schema="deliver_result.v1",
        permitted_tools=(Capability.READ_REPOSITORY, Capability.WRITE_ARTIFACT),
        retry=_BOUNDED_RETRY,
        timeout_seconds=1800,
        required_artifact_types=(ArtifactKind.DELIVERY_RECORD,),
        approval=ApprovalRequirement.OPTIONAL,
    ),
    ExecutorDeclaration(
        kind=ExecutorKind.DELIVER_DEPLOYMENT,
        phase=LifecyclePhase.DELIVER,
        input_schema="deliver_request.v1",
        output_schema="deliver_result.v1",
        permitted_tools=(Capability.READ_REPOSITORY, Capability.PUBLISH_DEPLOYMENT),
        retry=_SINGLE_ATTEMPT,
        timeout_seconds=3600,
        required_artifact_types=(ArtifactKind.DEPLOYMENT_RECORD,),
        approval=ApprovalRequirement.ALWAYS,
    ),
)


EXECUTOR_REGISTRY: Final[Mapping[ExecutorKind, ExecutorDeclaration]] = MappingProxyType(
    {declaration.kind: declaration for declaration in _DECLARATIONS}
)


def default_executor_for_phase(phase: LifecyclePhase) -> ExecutorKind:
    """The executor a milestone gets when the document names none.

    Every phase has exactly one obvious default so an author is not forced to
    name an executor to get correct behavior, and no phase can end up with a
    milestone that has no executor at all.
    """

    defaults: Mapping[LifecyclePhase, ExecutorKind] = {
        LifecyclePhase.CLARIFY: ExecutorKind.CLARIFY_REQUIREMENTS,
        LifecyclePhase.VALIDATE: ExecutorKind.VALIDATE_REPOSITORY,
        LifecyclePhase.PLAN: ExecutorKind.PLAN_IMPLEMENTATION,
        LifecyclePhase.IMPLEMENT: ExecutorKind.IMPLEMENT_CODE_CHANGE,
        LifecyclePhase.VERIFY: ExecutorKind.VERIFY_TESTS,
        LifecyclePhase.REVIEW: ExecutorKind.REVIEW_AGENT,
        LifecyclePhase.DELIVER: ExecutorKind.DELIVER_ARTIFACT,
    }
    return defaults[phase]


def lookup_executor(raw: str) -> ExecutorDeclaration | None:
    """Resolve a declared executor name, or ``None`` when it is not registered."""

    try:
        kind = ExecutorKind(raw.strip())
    except ValueError:
        return None
    return EXECUTOR_REGISTRY[kind]


__all__ = [
    "EXECUTOR_REGISTRY",
    "ApprovalRequirement",
    "ExecutorDeclaration",
    "ExecutorKind",
    "RetryPolicy",
    "default_executor_for_phase",
    "lookup_executor",
]
