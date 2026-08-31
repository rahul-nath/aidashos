# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The compiled plan contract: the only thing a running WorkUnit obeys.

A compiled plan is immutable, deterministically serializable, and content
addressed. Two compilations of the same DesignDoc revision by the same compiler
version produce byte-identical canonical JSON and therefore the same
``plan_hash``; changing any authority-bearing field changes the hash. That is
what lets a root workflow refuse to run against a plan it was not started with.

Nothing in the hashed content may be generated at compile time: no timestamps, no
row identifiers, no model prose in an authority-bearing field. The plan describes
work, and the identity it carries is derived from its source revision rather than
from the WorkUnit row that later points at it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal

from ..ids import sha256_text
from .executors import ExecutorKind
from .lifecycle import (
    LIFECYCLE_PROFILE,
    LIFECYCLE_PROFILE_VERSION,
    ORDERED_PHASES,
    FailureClass,
    LifecyclePhase,
    phase_ordinal,
)
from .retry import (
    RetryPolicy,
    legacy_max_attempts,
    retry_policy_from_legacy_max_attempts,
    retry_policy_from_payload,
)

SCHEMA_VERSION_COMPILED_WORK_PLAN: Final = "compiled_work_plan.v5"
COMPILER_VERSION: Final = "design_doc_compiler.v4"
LEGACY_SCHEMA_VERSION_COMPILED_WORK_PLAN: Final = "compiled_work_plan.v3"
_LEGACY_RETRY_POLICY_SCHEMA_VERSIONS: Final = frozenset(
    {LEGACY_SCHEMA_VERSION_COMPILED_WORK_PLAN, "compiled_work_plan.v4"}
)


def canonical_json(payload: Any) -> str:
    """Serialize deterministically.

    Sorted keys give a stable field order without a hand-maintained list that
    could drift from the dataclass. Compact separators keep the bytes that get
    hashed independent of formatting preference.
    """

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class SourceProvenance:
    """Where in the source document an executable field came from."""

    design_doc_id: str
    design_doc_revision_id: str
    source_heading: str
    source_start: int
    source_end: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "design_doc_id": self.design_doc_id,
            "design_doc_revision_id": self.design_doc_revision_id,
            "source_heading": self.source_heading,
            "source_start": self.source_start,
            "source_end": self.source_end,
        }


@dataclass(frozen=True)
class ToolPolicy:
    permitted_tools: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {"permitted_tools": [str(item) for item in self.permitted_tools]}


@dataclass(frozen=True)
class ApprovalPolicy:
    """Whether an operator must approve, and what they are approving.

    ``required`` is compiled, never inferred at runtime. A missing approval can
    only ever mean "not yet granted", so the absence of a decision cannot read as
    a decision.
    """

    required: bool
    prompt: str

    def to_payload(self) -> dict[str, Any]:
        return {"required": self.required, "prompt": self.prompt}


@dataclass(frozen=True)
class PermissionPolicy:
    """The plan-level ceiling that every milestone and spawned task must obey."""

    autonomous_capabilities: tuple[str, ...]
    approval_required_capabilities: tuple[str, ...]
    denied_capabilities: tuple[str, ...]

    @property
    def capability_ceiling(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    *self.autonomous_capabilities,
                    *self.approval_required_capabilities,
                }
            )
        )

    @property
    def requires_start_approval(self) -> bool:
        return bool(self.approval_required_capabilities)

    def to_payload(self) -> dict[str, Any]:
        return {
            "autonomous_capabilities": sorted(self.autonomous_capabilities),
            "approval_required_capabilities": sorted(self.approval_required_capabilities),
            "denied_capabilities": sorted(self.denied_capabilities),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> PermissionPolicy:
        return cls(
            autonomous_capabilities=tuple(str(item) for item in payload["autonomous_capabilities"]),
            approval_required_capabilities=tuple(
                str(item) for item in payload["approval_required_capabilities"]
            ),
            denied_capabilities=tuple(str(item) for item in payload["denied_capabilities"]),
        )


@dataclass(frozen=True)
class RequiredDelivery:
    """Success means every named terminal artifact was recorded."""

    kind: Literal["required"] = "required"
    artifact_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.artifact_types:
            raise ValueError("a required delivery contract must name at least one artifact")

    def to_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "artifact_types": sorted(self.artifact_types)}


@dataclass(frozen=True)
class NoDeliveryRequired:
    """An advisory-only plan explicitly states why it has no delivery artifact."""

    kind: Literal["none"] = "none"
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("a no-delivery contract must state why no delivery is required")

    def to_payload(self) -> dict[str, str]:
        return {"kind": self.kind, "reason": self.reason}


@dataclass(frozen=True)
class LegacyUnspecifiedDelivery:
    """A persisted v3 plan whose empty terminal gate had no stated meaning."""

    kind: Literal["legacy_unspecified"] = "legacy_unspecified"

    def to_payload(self) -> dict[str, str]:
        return {"kind": self.kind}


DeliveryContract = RequiredDelivery | NoDeliveryRequired | LegacyUnspecifiedDelivery


def delivery_contract_from_payload(payload: Mapping[str, Any]) -> DeliveryContract:
    kind = str(payload["kind"])
    if kind == "required":
        return RequiredDelivery(
            artifact_types=tuple(str(item) for item in payload["artifact_types"])
        )
    if kind == "none":
        return NoDeliveryRequired(reason=str(payload["reason"]))
    if kind == "legacy_unspecified":
        return LegacyUnspecifiedDelivery()
    raise ValueError(f"unknown delivery contract kind {kind!r}")


@dataclass(frozen=True)
class FailurePolicy:
    """What a failure of this milestone means for the phase and the WorkUnit."""

    default_class: FailureClass
    retry_policy: RetryPolicy
    blocks_phase: bool

    def to_payload(self, *, legacy_retry_policy: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "default_class": self.default_class.value,
            "blocks_phase": self.blocks_phase,
        }
        if legacy_retry_policy:
            payload["max_attempts"] = legacy_max_attempts(self.retry_policy)
        else:
            payload["retry_policy"] = self.retry_policy.to_payload()
        return payload


@dataclass(frozen=True)
class CompiledMilestone:
    stable_key: str
    title: str
    description: str
    phase: LifecyclePhase
    ordinal: int
    dependencies: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    required_artifacts: tuple[str, ...]
    executor_kind: ExecutorKind
    tool_policy: ToolPolicy
    approval_policy: ApprovalPolicy
    failure_policy: FailurePolicy
    source_provenance: SourceProvenance
    timeout_seconds: int
    phase_inferred: bool

    def to_payload(self, *, legacy_retry_policy: bool = False) -> dict[str, Any]:
        return {
            "stable_key": self.stable_key,
            "title": self.title,
            "description": self.description,
            "phase": self.phase.value,
            "ordinal": self.ordinal,
            "dependencies": sorted(self.dependencies),
            "acceptance_criteria": list(self.acceptance_criteria),
            "required_artifacts": sorted(self.required_artifacts),
            "executor_kind": self.executor_kind.value,
            "tool_policy": self.tool_policy.to_payload(),
            "approval_policy": self.approval_policy.to_payload(),
            "failure_policy": self.failure_policy.to_payload(
                legacy_retry_policy=legacy_retry_policy
            ),
            "source_provenance": self.source_provenance.to_payload(),
            "timeout_seconds": self.timeout_seconds,
            "phase_inferred": self.phase_inferred,
        }

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any], *, legacy_retry_policy: bool = False
    ) -> CompiledMilestone:
        failure_policy = payload["failure_policy"]
        return cls(
            stable_key=str(payload["stable_key"]),
            title=str(payload["title"]),
            description=str(payload["description"]),
            phase=LifecyclePhase(str(payload["phase"])),
            ordinal=int(payload["ordinal"]),
            dependencies=tuple(str(item) for item in payload["dependencies"]),
            acceptance_criteria=tuple(str(item) for item in payload["acceptance_criteria"]),
            required_artifacts=tuple(str(item) for item in payload["required_artifacts"]),
            executor_kind=ExecutorKind(str(payload["executor_kind"])),
            tool_policy=ToolPolicy(
                tuple(str(item) for item in payload["tool_policy"]["permitted_tools"])
            ),
            approval_policy=ApprovalPolicy(
                required=bool(payload["approval_policy"]["required"]),
                prompt=str(payload["approval_policy"]["prompt"]),
            ),
            failure_policy=FailurePolicy(
                default_class=FailureClass(str(failure_policy["default_class"])),
                retry_policy=(
                    retry_policy_from_legacy_max_attempts(int(failure_policy["max_attempts"]))
                    if legacy_retry_policy
                    else retry_policy_from_payload(failure_policy["retry_policy"])
                ),
                blocks_phase=bool(failure_policy["blocks_phase"]),
            ),
            source_provenance=SourceProvenance(
                design_doc_id=str(payload["source_provenance"]["design_doc_id"]),
                design_doc_revision_id=str(payload["source_provenance"]["design_doc_revision_id"]),
                source_heading=str(payload["source_provenance"]["source_heading"]),
                source_start=int(payload["source_provenance"]["source_start"]),
                source_end=int(payload["source_provenance"]["source_end"]),
            ),
            timeout_seconds=int(payload["timeout_seconds"]),
            phase_inferred=bool(payload["phase_inferred"]),
        )


@dataclass(frozen=True)
class DependencyEdge:
    milestone_key: str
    depends_on_key: str

    def to_payload(self) -> dict[str, str]:
        return {"milestone_key": self.milestone_key, "depends_on_key": self.depends_on_key}


@dataclass(frozen=True)
class PlanSource:
    design_doc_id: str
    design_doc_revision_id: str
    content_hash: str

    def to_payload(self) -> dict[str, str]:
        return {
            "design_doc_id": self.design_doc_id,
            "design_doc_revision_id": self.design_doc_revision_id,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class DocumentContext:
    """What the document said, carried to whoever executes it.

    Every one of these was parsed and then dropped at compile time, so an agent
    running a milestone saw its own title and acceptance criteria and nothing
    else: not why the work exists, not the failure that matters most, not the
    constraints it must not violate. The document that authorized the work was
    invisible to the work.

    These are the parsed collections rather than the raw document on purpose. Raw
    text would put an unbounded blob in a hashed plan and in every prompt; the
    collections are already structured, already bounded by the sections that
    produced them, and stable enough to hash.
    """

    motivation: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    failure_modes: tuple[str, ...] = ()
    rollout: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "motivation": list(self.motivation),
            "requirements": list(self.requirements),
            "non_goals": list(self.non_goals),
            "constraints": list(self.constraints),
            "assumptions": list(self.assumptions),
            "acceptance_criteria": list(self.acceptance_criteria),
            "failure_modes": list(self.failure_modes),
            "rollout": list(self.rollout),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> DocumentContext:
        def read(name: str) -> tuple[str, ...]:
            return tuple(str(item) for item in payload.get(name, ()))

        return cls(
            motivation=read("motivation"),
            requirements=read("requirements"),
            non_goals=read("non_goals"),
            constraints=read("constraints"),
            assumptions=read("assumptions"),
            acceptance_criteria=read("acceptance_criteria"),
            failure_modes=read("failure_modes"),
            rollout=read("rollout"),
        )

    def render(self) -> str:
        """The document's own words, as a prompt section.

        Rendering lives here rather than in the executor so the plan decides how
        its context reads. An empty document renders as the empty string, which
        keeps a prompt from carrying headings with nothing under them.
        """

        blocks = [
            ("Why this work exists", self.motivation),
            ("What the document requires", self.requirements),
            ("Explicitly out of scope", self.non_goals),
            ("Constraints that bind every milestone", self.constraints),
            ("Assumptions the document is working from", self.assumptions),
            ("What the document accepts as done", self.acceptance_criteria),
            ("Failure modes that matter most", self.failure_modes),
            ("Rollout and rollback", self.rollout),
        ]
        rendered = [
            f"{heading}:\n" + "\n".join(f"- {item}" for item in items)
            for heading, items in blocks
            if items
        ]
        return "\n\n".join(rendered)


@dataclass(frozen=True)
class LifecycleBinding:
    profile: str
    profile_version: int
    ordered_phases: tuple[LifecyclePhase, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "profile_version": self.profile_version,
            "ordered_phases": [phase.value for phase in self.ordered_phases],
        }


class DeliveryPace(StrEnum):
    """How much concurrency the document asks to spend compressing wall clock.

    A closed set rather than a date. A date written into a durable document is
    wrong the week after it is written and then quietly means something else,
    while a pace still means what it meant; and a plan is hashed, so a field that
    rots is a field that invalidates the hash for no behavioural reason.

    This is the restriction ``executors.py`` already applies to behaviour, in the
    other dimension: the document chooses among paces the runtime already trusts
    and cannot describe a new one. It is a request in every case. What it asks
    for is reconciled against ``AuthorityPolicy.max_parallel_milestones`` by
    ``resolve_schedule_width``, which the document cannot raise.
    """

    UNSPECIFIED = "unspecified"
    DELIBERATE = "deliberate"
    STEADY = "steady"
    COMPRESSED = "compressed"


# ``None`` means the pace states no width of its own, so the authority ceiling
# stands unreduced. ``UNSPECIFIED`` maps to it because a silent document must
# schedule exactly as it did before this field existed. ``COMPRESSED`` maps to
# it as well, and the collision is the honest part: asking to go as fast as
# possible cannot mean more than the ceiling allows, so the two differ in what
# they say and not in what they get. ``ScheduleWidth.binding_constraint``
# reports which one was actually in force.
DELIVERY_PACE_WIDTH_REQUEST: Final[Mapping[DeliveryPace, int | None]] = {
    DeliveryPace.UNSPECIFIED: None,
    DeliveryPace.DELIBERATE: 1,
    DeliveryPace.STEADY: 2,
    DeliveryPace.COMPRESSED: None,
}


@dataclass(frozen=True)
class AuthorityPolicy:
    """The bounds the document could not widen even by asking.

    These are compiled in from the runtime, not read from the document. A plan
    carries them so an auditor reading a persisted plan can see exactly which
    bounds that execution ran under.
    """

    max_parallel_milestones: int
    operator_approval_inferrable: bool
    document_may_define_phases: bool
    document_may_supply_code: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "max_parallel_milestones": self.max_parallel_milestones,
            "operator_approval_inferrable": self.operator_approval_inferrable,
            "document_may_define_phases": self.document_may_define_phases,
            "document_may_supply_code": self.document_may_supply_code,
        }


DEFAULT_AUTHORITY_POLICY: Final = AuthorityPolicy(
    max_parallel_milestones=4,
    operator_approval_inferrable=False,
    document_may_define_phases=False,
    document_may_supply_code=False,
)


@dataclass(frozen=True)
class CompiledWorkPlan:
    """One immutable execution input.

    ``plan_hash`` is derived, not stored authority: ``compute_plan_hash`` recomputes
    it from the canonical payload, so a tampered row cannot claim a hash it does
    not have.
    """

    schema_version: str
    compiler_version: str
    work_unit_identity: str
    source: PlanSource
    lifecycle: LifecycleBinding
    milestones: tuple[CompiledMilestone, ...]
    dependency_edges: tuple[DependencyEdge, ...]
    document_context: DocumentContext
    authority_policy: AuthorityPolicy
    delivery_contract: DeliveryContract
    target_project_id: str
    """Which registered project this work is about.

    One line of intent, plan-level because one WorkUnit is about one repository.
    It is a registry key rather than a path on purpose: the row it resolves to
    carries `read_only`, `owns`, `avoid`, and the verification commands, and a
    path would arrive with none of them, which is exactly how the read-only gate
    at `dispatcher_runner.py` would get bypassed.

    A worktree path stays out of the plan for a different reason: a retry
    produces a different path, and the plan must hash the same across retries.
    """

    declared_delivery_pace: DeliveryPace = DeliveryPace.UNSPECIFIED
    """The pace the document asked for, carried so the schedule is auditable.

    Authority-bearing, so it is hashed: it changes how many milestones run at
    once, and an auditor reading a persisted plan must be able to see the pace
    that execution actually ran under rather than infer it from the document.

    It is keyed out of the payload entirely when ``UNSPECIFIED``. Every plan
    compiled before this field existed was compiled from a document that could
    not state a pace, so a silent document has to hash exactly as it did then or
    every plan already in the ledger fails ``PlanIntegrityError`` on the next
    load. Absent and ``UNSPECIFIED`` are the same fact, and only one of them can
    be the one that hashes.
    """

    permission_policy: PermissionPolicy | None = None

    @property
    def required_final_artifacts(self) -> tuple[str, ...]:
        """Compatibility read for callers that only need the required set."""

        if isinstance(self.delivery_contract, RequiredDelivery):
            return self.delivery_contract.artifact_types
        return ()

    def hashable_payload(self) -> dict[str, Any]:
        """The payload the hash covers: everything authority-bearing, nothing else."""

        if self.declared_delivery_pace is not DeliveryPace.UNSPECIFIED:
            return {
                **self._base_hashable_payload(),
                "declared_delivery_pace": self.declared_delivery_pace.value,
            }
        return self._base_hashable_payload()

    def _base_hashable_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "compiler_version": self.compiler_version,
            "work_unit_identity": self.work_unit_identity,
            "target_project_id": self.target_project_id,
            "source": self.source.to_payload(),
            "lifecycle": self.lifecycle.to_payload(),
            "milestones": [
                milestone.to_payload(
                    legacy_retry_policy=self.schema_version in _LEGACY_RETRY_POLICY_SCHEMA_VERSIONS
                )
                for milestone in self.ordered_milestones()
            ],
            "dependency_edges": [edge.to_payload() for edge in self.ordered_dependency_edges()],
            "document_context": self.document_context.to_payload(),
            "authority_policy": self.authority_policy.to_payload(),
        }
        if self.schema_version == LEGACY_SCHEMA_VERSION_COMPILED_WORK_PLAN:
            payload["required_final_artifacts"] = sorted(self.required_final_artifacts)
            return payload
        payload["delivery_contract"] = self.delivery_contract.to_payload()
        if self.permission_policy is not None:
            payload["permission_policy"] = self.permission_policy.to_payload()
        return payload

    def ordered_milestones(self) -> tuple[CompiledMilestone, ...]:
        """Milestones in lifecycle order, then declared order, then key.

        A total order with no ties is what makes the serialization canonical; two
        milestones sharing a phase and ordinal still serialize identically every
        time because the key breaks the tie.
        """

        return tuple(
            sorted(
                self.milestones,
                key=lambda item: (phase_ordinal(item.phase), item.ordinal, item.stable_key),
            )
        )

    def ordered_dependency_edges(self) -> tuple[DependencyEdge, ...]:
        return tuple(
            sorted(
                self.dependency_edges,
                key=lambda edge: (edge.milestone_key, edge.depends_on_key),
            )
        )

    def canonical_json(self) -> str:
        return canonical_json(self.hashable_payload())

    def plan_hash(self) -> str:
        return sha256_text(self.canonical_json())

    def to_payload(self) -> dict[str, Any]:
        payload = self.hashable_payload()
        payload["plan_hash"] = self.plan_hash()
        return payload

    def milestones_in_phase(self, phase: LifecyclePhase) -> tuple[CompiledMilestone, ...]:
        return tuple(item for item in self.ordered_milestones() if item.phase is phase)

    def milestone(self, stable_key: str) -> CompiledMilestone:
        for item in self.milestones:
            if item.stable_key == stable_key:
                return item
        raise KeyError(f"compiled plan has no milestone {stable_key!r}")

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CompiledWorkPlan:
        """Rebuild a plan from its persisted canonical JSON.

        Pure and side-effect free, so a deterministic workflow body may call it on
        a payload a step loaded.
        """

        schema_version = str(payload["schema_version"])
        plan = cls(
            schema_version=schema_version,
            compiler_version=str(payload["compiler_version"]),
            work_unit_identity=str(payload["work_unit_identity"]),
            target_project_id=str(payload["target_project_id"]),
            source=PlanSource(
                design_doc_id=str(payload["source"]["design_doc_id"]),
                design_doc_revision_id=str(payload["source"]["design_doc_revision_id"]),
                content_hash=str(payload["source"]["content_hash"]),
            ),
            lifecycle=LifecycleBinding(
                profile=str(payload["lifecycle"]["profile"]),
                profile_version=int(payload["lifecycle"]["profile_version"]),
                ordered_phases=tuple(
                    LifecyclePhase(str(item)) for item in payload["lifecycle"]["ordered_phases"]
                ),
            ),
            milestones=tuple(
                CompiledMilestone.from_payload(
                    item,
                    legacy_retry_policy=schema_version in _LEGACY_RETRY_POLICY_SCHEMA_VERSIONS,
                )
                for item in payload["milestones"]
            ),
            dependency_edges=tuple(
                DependencyEdge(
                    milestone_key=str(item["milestone_key"]),
                    depends_on_key=str(item["depends_on_key"]),
                )
                for item in payload["dependency_edges"]
            ),
            document_context=DocumentContext.from_payload(payload["document_context"]),
            authority_policy=AuthorityPolicy(
                max_parallel_milestones=int(payload["authority_policy"]["max_parallel_milestones"]),
                operator_approval_inferrable=bool(
                    payload["authority_policy"]["operator_approval_inferrable"]
                ),
                document_may_define_phases=bool(
                    payload["authority_policy"]["document_may_define_phases"]
                ),
                document_may_supply_code=bool(
                    payload["authority_policy"]["document_may_supply_code"]
                ),
            ),
            delivery_contract=(
                delivery_contract_from_payload(payload["delivery_contract"])
                if "delivery_contract" in payload
                else (
                    RequiredDelivery(
                        artifact_types=tuple(
                            str(item) for item in payload["required_final_artifacts"]
                        )
                    )
                    if payload.get("required_final_artifacts")
                    else LegacyUnspecifiedDelivery()
                )
            ),
            declared_delivery_pace=DeliveryPace(
                str(payload.get("declared_delivery_pace", DeliveryPace.UNSPECIFIED.value))
            ),
            permission_policy=(
                PermissionPolicy.from_payload(payload["permission_policy"])
                if payload.get("permission_policy") is not None
                else None
            ),
        )
        declared_hash = payload.get("plan_hash")
        if declared_hash is not None and str(declared_hash) != plan.plan_hash():
            raise PlanIntegrityError(
                expected=str(declared_hash),
                actual=plan.plan_hash(),
            )
        return plan


class PlanIntegrityError(RuntimeError):
    """A persisted plan does not hash to the hash stored beside it.

    Fail closed: the bytes an execution was authorized against are not the bytes
    on disk, and no amount of retrying makes that safe.
    """

    def __init__(self, *, expected: str, actual: str) -> None:
        super().__init__(f"compiled plan hash mismatch: expected {expected}, computed {actual}")
        self.expected = expected
        self.actual = actual


def default_lifecycle_binding() -> LifecycleBinding:
    return LifecycleBinding(
        profile=LIFECYCLE_PROFILE,
        profile_version=LIFECYCLE_PROFILE_VERSION,
        ordered_phases=ORDERED_PHASES,
    )


__all__ = [
    "COMPILER_VERSION",
    "DEFAULT_AUTHORITY_POLICY",
    "DELIVERY_PACE_WIDTH_REQUEST",
    "SCHEMA_VERSION_COMPILED_WORK_PLAN",
    "ApprovalPolicy",
    "AuthorityPolicy",
    "CompiledMilestone",
    "CompiledWorkPlan",
    "DeliveryPace",
    "DependencyEdge",
    "DocumentContext",
    "DeliveryContract",
    "FailurePolicy",
    "LifecycleBinding",
    "LegacyUnspecifiedDelivery",
    "NoDeliveryRequired",
    "PlanIntegrityError",
    "PlanSource",
    "PermissionPolicy",
    "RequiredDelivery",
    "SourceProvenance",
    "ToolPolicy",
    "canonical_json",
    "default_lifecycle_binding",
]
