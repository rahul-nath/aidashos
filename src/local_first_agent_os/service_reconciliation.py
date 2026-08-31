# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A typed decision engine for reconciling resident services.

The engine knows what every resident service has in common: observe it, compare
the observation with a declared contract, choose one lifecycle action, execute
that action, and prove the contract afterwards.  It deliberately knows nothing
about Pi, launchd, ports, or pid files.  Those are adapter facts supplied by a
service instance.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, assert_never


class ServiceName(StrEnum):
    """Resident services whose lifecycle is managed by this application."""

    PI_DAEMON = "pi-daemon"


@dataclass(frozen=True)
class RequiredHealthField:
    name: str
    expected: str


@dataclass(frozen=True)
class ServiceContract:
    name: ServiceName
    required_health: tuple[RequiredHealthField, ...]
    require_supervisor_ownership: bool
    readiness_timeout_seconds: float = 40.0
    poll_interval_seconds: float = 0.25


@dataclass(frozen=True)
class ServiceUnavailable:
    pass


@dataclass(frozen=True)
class ServiceHealthy:
    health: Mapping[str, object]
    supervisor_owned: bool


ServiceObservation = ServiceUnavailable | ServiceHealthy


@dataclass(frozen=True)
class HealthMismatch:
    field: str
    expected: str
    actual: str | None


@dataclass(frozen=True)
class ServiceSatisfied:
    observation: ServiceHealthy


@dataclass(frozen=True)
class StartService:
    pass


@dataclass(frozen=True)
class RestartService:
    mismatches: tuple[HealthMismatch, ...]
    ownership_mismatch: bool


ServicePlan = ServiceSatisfied | StartService | RestartService


class ServiceProbe(Protocol):
    def observe(self) -> ServiceObservation: ...


class ServiceController(Protocol):
    def start(self) -> None: ...

    def restart(self) -> None: ...


class ServiceReconciliationFailed(RuntimeError):
    pass


def plan_service_reconciliation(
    contract: ServiceContract,
    observation: ServiceObservation,
) -> ServicePlan:
    """Return the only lifecycle action permitted by the current facts."""

    match observation:
        case ServiceUnavailable():
            return StartService()
        case ServiceHealthy(health=health, supervisor_owned=supervisor_owned):
            mismatches = tuple(
                HealthMismatch(
                    field=requirement.name,
                    expected=requirement.expected,
                    actual=(
                        None
                        if health.get(requirement.name) is None
                        else str(health[requirement.name])
                    ),
                )
                for requirement in contract.required_health
                if str(health.get(requirement.name, "")) != requirement.expected
            )
            ownership_mismatch = contract.require_supervisor_ownership and not supervisor_owned
            if mismatches or ownership_mismatch:
                return RestartService(
                    mismatches=mismatches,
                    ownership_mismatch=ownership_mismatch,
                )
            return ServiceSatisfied(observation)
    assert_never(observation)


def reconcile_service(
    contract: ServiceContract,
    probe: ServiceProbe,
    controller: ServiceController,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ServiceHealthy:
    """Execute one plan and return only after the declared contract is true."""

    initial = probe.observe()
    plan = plan_service_reconciliation(contract, initial)
    match plan:
        case ServiceSatisfied(observation=observation):
            return observation
        case StartService():
            controller.start()
        case RestartService():
            controller.restart()
        case _:
            assert_never(plan)

    deadline = monotonic() + contract.readiness_timeout_seconds
    last_plan: ServicePlan = plan
    while monotonic() < deadline:
        observation = probe.observe()
        last_plan = plan_service_reconciliation(contract, observation)
        if isinstance(last_plan, ServiceSatisfied):
            return last_plan.observation
        sleep(contract.poll_interval_seconds)

    raise ServiceReconciliationFailed(
        f"{contract.name.value} did not satisfy its service contract within "
        f"{contract.readiness_timeout_seconds:g}s; last state was {last_plan!r}"
    )


__all__ = [
    "HealthMismatch",
    "RequiredHealthField",
    "RestartService",
    "ServiceContract",
    "ServiceController",
    "ServiceHealthy",
    "ServiceName",
    "ServiceObservation",
    "ServicePlan",
    "ServiceProbe",
    "ServiceReconciliationFailed",
    "ServiceSatisfied",
    "ServiceUnavailable",
    "StartService",
    "plan_service_reconciliation",
    "reconcile_service",
]
