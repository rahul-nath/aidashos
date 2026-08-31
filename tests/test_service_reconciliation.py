# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass, field

from local_first_agent_os.service_reconciliation import (
    RequiredHealthField,
    RestartService,
    ServiceContract,
    ServiceHealthy,
    ServiceName,
    ServiceSatisfied,
    ServiceUnavailable,
    StartService,
    plan_service_reconciliation,
    reconcile_service,
)

CONTRACT = ServiceContract(
    name=ServiceName.PI_DAEMON,
    required_health=(
        RequiredHealthField("service_name", "pi-daemon"),
        RequiredHealthField("runtime_revision", "expected"),
    ),
    require_supervisor_ownership=True,
    readiness_timeout_seconds=1,
    poll_interval_seconds=0,
)


def test_plan_starts_an_unavailable_service() -> None:
    assert isinstance(
        plan_service_reconciliation(CONTRACT, ServiceUnavailable()),
        StartService,
    )


def test_plan_names_health_and_ownership_drift() -> None:
    plan = plan_service_reconciliation(
        CONTRACT,
        ServiceHealthy(
            health={"service_name": "pi-daemon", "runtime_revision": "stale"},
            supervisor_owned=False,
        ),
    )

    assert isinstance(plan, RestartService)
    assert plan.ownership_mismatch is True
    assert [(item.field, item.actual) for item in plan.mismatches] == [
        ("runtime_revision", "stale")
    ]


def test_plan_keeps_only_a_service_that_satisfies_the_whole_contract() -> None:
    observation = ServiceHealthy(
        health={"service_name": "pi-daemon", "runtime_revision": "expected"},
        supervisor_owned=True,
    )

    assert plan_service_reconciliation(CONTRACT, observation) == ServiceSatisfied(observation)


@dataclass
class _Probe:
    observations: list[ServiceUnavailable | ServiceHealthy]

    def observe(self) -> ServiceUnavailable | ServiceHealthy:
        if len(self.observations) == 1:
            return self.observations[0]
        return self.observations.pop(0)


@dataclass
class _Controller:
    actions: list[str] = field(default_factory=list)

    def start(self) -> None:
        self.actions.append("start")

    def restart(self) -> None:
        self.actions.append("restart")


def test_reconcile_executes_one_action_then_proves_readiness() -> None:
    ready = ServiceHealthy(
        health={"service_name": "pi-daemon", "runtime_revision": "expected"},
        supervisor_owned=True,
    )
    probe = _Probe([ServiceUnavailable(), ready])
    controller = _Controller()

    result = reconcile_service(
        CONTRACT,
        probe,
        controller,
        monotonic=iter((0.0, 0.0)).__next__,
        sleep=lambda _seconds: None,
    )

    assert result == ready
    assert controller.actions == ["start"]
