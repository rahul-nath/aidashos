# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One capability compatibility decision shared by declarations and launchers.

An admission preserves a grant; it never creates authority or widens a ceiling.
Its proof covers driver compatibility only, not actor authentication, source
identity, host containment, approval, or the truth of execution evidence.
Those boundaries must still prove their own prerequisites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final, assert_never

from .capabilities import Capability

if TYPE_CHECKING:
    from .spawn_authority import SpawnAuthority


class ExecutionDriver(StrEnum):
    PLANNED_DISPATCH = "planned_dispatch"
    REGISTERED_VERIFICATION = "registered_verification"
    DELIVERY_RECORD = "delivery_record"
    OPERATOR_DECISION = "operator_decision"
    CODEX_INSPECTION = "codex_inspection"
    CODEX_TOOL_PREFLIGHT = "codex_tool_preflight"


@dataclass(frozen=True)
class ExecutionContract:
    """A closed driver selection, without caller-defined permission rules."""

    driver: ExecutionDriver

    def __post_init__(self) -> None:
        if not isinstance(self.driver, ExecutionDriver):
            raise TypeError("execution driver must be an ExecutionDriver")


@dataclass(frozen=True)
class _DriverPolicy:
    required: tuple[Capability, ...]
    permitted: frozenset[Capability]


def _driver_policy(driver: ExecutionDriver) -> _DriverPolicy:
    """The only owner of required and forbidden driver capabilities."""

    if not isinstance(driver, ExecutionDriver):
        raise TypeError("execution driver must be an ExecutionDriver")
    match driver:
        case ExecutionDriver.PLANNED_DISPATCH:
            # Dispatch selects role-specific children. Each concrete child must
            # obtain its own admission after intersecting role with this ceiling.
            return _DriverPolicy(
                (Capability.READ_REPOSITORY, Capability.INVOKE_MODEL), frozenset(Capability)
            )
        case ExecutionDriver.REGISTERED_VERIFICATION:
            required = (Capability.READ_REPOSITORY, Capability.RUN_COMMAND)
            return _DriverPolicy(required, frozenset(required))
        case ExecutionDriver.DELIVERY_RECORD:
            required = (Capability.READ_REPOSITORY, Capability.WRITE_ARTIFACT)
            return _DriverPolicy(required, frozenset(required))
        case ExecutionDriver.OPERATOR_DECISION:
            required = (Capability.READ_REPOSITORY,)
            return _DriverPolicy(required, frozenset(required))
        case ExecutionDriver.CODEX_INSPECTION | ExecutionDriver.CODEX_TOOL_PREFLIGHT:
            required = (
                (Capability.READ_REPOSITORY, Capability.INVOKE_MODEL)
                if driver is ExecutionDriver.CODEX_INSPECTION
                else (Capability.READ_REPOSITORY,)
            )
            return _DriverPolicy(
                required,
                frozenset(
                    (Capability.READ_REPOSITORY, Capability.INVOKE_MODEL, Capability.ASK_OPERATOR)
                ),
            )
    assert_never(driver)


def required_capabilities_for(driver: ExecutionDriver) -> frozenset[Capability]:
    return frozenset(_driver_policy(driver).required)


def declared_capabilities_for(driver: ExecutionDriver) -> tuple[Capability, ...]:
    """The minimum complete declaration in stable serialized order."""

    return _driver_policy(driver).required


class ExecutionAdmissionFailure(StrEnum):
    EXECUTOR_RUNTIME_INCOMPATIBLE = "EXECUTOR_RUNTIME_INCOMPATIBLE"


@dataclass(frozen=True)
class ExecutionAdmissionRefusal:
    driver: ExecutionDriver
    missing_capabilities: frozenset[Capability]
    forbidden_capabilities: frozenset[Capability]

    def __post_init__(self) -> None:
        if not isinstance(self.driver, ExecutionDriver):
            raise TypeError("refusal driver must be an ExecutionDriver")
        _validate_capabilities(self.missing_capabilities)
        _validate_capabilities(self.forbidden_capabilities)
        if not self.missing_capabilities and not self.forbidden_capabilities:
            raise ValueError("a capability refusal must name the incompatible authority")

    @property
    def code(self) -> ExecutionAdmissionFailure:
        return ExecutionAdmissionFailure.EXECUTOR_RUNTIME_INCOMPATIBLE

    @property
    def reason(self) -> str:
        reasons: list[str] = []
        if self.missing_capabilities:
            reasons.append(
                "missing " + ", ".join(sorted(item.value for item in self.missing_capabilities))
            )
        if self.forbidden_capabilities:
            reasons.append(
                "forbidden " + ", ".join(sorted(item.value for item in self.forbidden_capabilities))
            )
        return f"{self.driver.value} authority is incompatible: {'; '.join(reasons)}"


@dataclass(frozen=True)
class ExecutionDriverRefusal:
    expected_driver: ExecutionDriver
    authorized_driver: ExecutionDriver

    def __post_init__(self) -> None:
        if not isinstance(self.expected_driver, ExecutionDriver) or not isinstance(
            self.authorized_driver, ExecutionDriver
        ):
            raise TypeError("driver refusal must contain ExecutionDriver members")
        if self.expected_driver is self.authorized_driver:
            raise ValueError("driver refusal requires a mismatched authorization")

    @property
    def code(self) -> ExecutionAdmissionFailure:
        return ExecutionAdmissionFailure.EXECUTOR_RUNTIME_INCOMPATIBLE

    @property
    def reason(self) -> str:
        return (
            f"execution authorization for {self.authorized_driver.value} "
            f"cannot authorize {self.expected_driver.value}"
        )


class ExecutionAdmissionError(ValueError):
    def __init__(self, refusal: ExecutionAdmissionRefusal | ExecutionDriverRefusal) -> None:
        self.refusal = refusal
        super().__init__(refusal.reason)


_ADMISSION_PROOF: Final = object()


@dataclass(frozen=True)
class AuthorizedExecution:
    """An exact grant checked for one driver by this module's admission owner."""

    contract: ExecutionContract
    authority: SpawnAuthority
    _proof: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _ADMISSION_PROOF:
            raise ValueError("execution authorization must come from the admission owner")
        refusal = _refusal_for(self.contract, self.authority)
        if refusal is not None:
            raise ExecutionAdmissionError(refusal)


def _validate_capabilities(capabilities: frozenset[Capability]) -> None:
    if not isinstance(capabilities, frozenset) or any(
        not isinstance(item, Capability) for item in capabilities
    ):
        raise TypeError("execution authority must contain only typed Capability members")


def _refusal_for(
    contract: ExecutionContract, authority: SpawnAuthority
) -> ExecutionAdmissionRefusal | None:
    # SpawnAuthority imports executor declarations, which consume this module.
    # Runtime import here keeps the dependency graph acyclic at registration.
    from .spawn_authority import SpawnAuthority

    if not isinstance(contract, ExecutionContract):
        raise TypeError("execution contract must be an ExecutionContract")
    if not isinstance(authority, SpawnAuthority):
        raise TypeError("execution authority must be a SpawnAuthority")
    _validate_capabilities(authority.capabilities)
    policy = _driver_policy(contract.driver)
    missing = frozenset(policy.required) - authority.capabilities
    forbidden = authority.capabilities - policy.permitted
    if missing or forbidden:
        return ExecutionAdmissionRefusal(contract.driver, missing, forbidden)
    return None


def admit_execution(
    contract: ExecutionContract, authority: SpawnAuthority
) -> AuthorizedExecution | ExecutionAdmissionRefusal:
    refusal = _refusal_for(contract, authority)
    if refusal is not None:
        return refusal
    return AuthorizedExecution(contract, authority, _ADMISSION_PROOF)


def require_authorized_execution(
    authorization: AuthorizedExecution, driver: ExecutionDriver
) -> AuthorizedExecution:
    """Consume a proof only for its own driver and recheck its exact grant."""

    if not isinstance(driver, ExecutionDriver):
        raise TypeError("expected execution driver must be an ExecutionDriver")
    if (
        not isinstance(authorization, AuthorizedExecution)
        or authorization._proof is not _ADMISSION_PROOF
    ):
        raise ValueError("execution authorization must come from the admission owner")
    if authorization.contract.driver is not driver:
        raise ExecutionAdmissionError(ExecutionDriverRefusal(driver, authorization.contract.driver))
    refusal = _refusal_for(authorization.contract, authorization.authority)
    if refusal is not None:
        raise ExecutionAdmissionError(refusal)
    return authorization


__all__ = [
    "AuthorizedExecution",
    "ExecutionAdmissionError",
    "ExecutionAdmissionFailure",
    "ExecutionAdmissionRefusal",
    "ExecutionContract",
    "ExecutionDriver",
    "ExecutionDriverRefusal",
    "admit_execution",
    "declared_capabilities_for",
    "required_capabilities_for",
    "require_authorized_execution",
]
