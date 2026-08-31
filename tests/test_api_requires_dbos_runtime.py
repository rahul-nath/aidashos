# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A failed DBOS launch must stop the API, not degrade it silently.

`launch_dbos` used to swallow every launch failure so the Pi direct path kept
working, and the API inherited that softness: a server whose runtime never
launched still answered every route while delivery, resume, and recovery were
unavailable. The scenarios in ``features/api_requires_dbos_runtime.feature``
pin the loud behavior; the unit tests below take one decision variable each
through `launch_dbos`'s typed outcome.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, scenarios, then, when

from local_first_agent_os import dbos_app
from local_first_agent_os.api import create_app
from local_first_agent_os.dbos_app import (
    DbosDisabled,
    DbosLaunched,
    DbosLaunchFailed,
    DbosUnavailable,
)

scenarios("features/api_requires_dbos_runtime.feature")


class _ExplodingDBOS:
    @staticmethod
    def launch() -> None:
        raise RuntimeError("system database is unreachable")


class _QuietDBOS:
    launches: int = 0

    @classmethod
    def launch(cls) -> None:
        cls.launches += 1


# --- gherkin steps ------------------------------------------------------------


@pytest.fixture()
def world() -> dict[str, Any]:
    return {}


@given("settings that require the durable runtime")
def _durable_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dbos_app, "_dbos_launched", False)
    monkeypatch.setattr(dbos_app, "settings", SimpleNamespace(use_dbos=True))


@given("settings that do not use the durable runtime")
def _direct_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dbos_app, "_dbos_launched", False)
    monkeypatch.setattr(dbos_app, "settings", SimpleNamespace(use_dbos=False))


@given("a durable runtime whose launch raises")
def _exploding_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dbos_app, "DBOS", _ExplodingDBOS)


@given("a durable runtime that launches cleanly")
def _quiet_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dbos_app, "DBOS", _QuietDBOS)


@when("the API starts")
def _the_api_starts(world: dict[str, Any], runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("local_first_agent_os.api.get_runtime", lambda: runtime)
    monkeypatch.setattr("local_first_agent_os.api.get_settings", lambda: runtime.settings)
    client = TestClient(create_app())
    try:
        with client:
            world["health"] = client.get("/health").json()
    except RuntimeError as error:
        world["startup_error"] = error


@then("startup is refused with a message naming the durable runtime")
def _startup_refused(world: dict[str, Any]) -> None:
    assert "health" not in world
    assert "durable runtime" in str(world["startup_error"])
    assert "did not launch" in str(world["startup_error"])


@then("the API serves and health reports the runtime is not launched")
def _serves_without_runtime(world: dict[str, Any]) -> None:
    assert "startup_error" not in world
    assert world["health"]["status"] == "ok"
    assert world["health"]["dbos_launched"] is False


@then("the API serves and health reports the runtime is launched")
def _serves_with_runtime(world: dict[str, Any]) -> None:
    assert "startup_error" not in world
    assert world["health"]["status"] == "ok"
    assert world["health"]["dbos_launched"] is True


# --- one decision variable each ----------------------------------------------


def test_a_launch_failure_is_a_typed_outcome_not_a_silent_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dbos_app, "_dbos_launched", False)
    monkeypatch.setattr(dbos_app, "settings", SimpleNamespace(use_dbos=True))
    monkeypatch.setattr(dbos_app, "DBOS", _ExplodingDBOS)

    outcome = dbos_app.launch_dbos()

    assert isinstance(outcome, DbosLaunchFailed)
    assert "system database is unreachable" in outcome.reason
    assert dbos_app.dbos_runtime_active() is False


def test_configured_off_reports_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dbos_app, "_dbos_launched", False)
    monkeypatch.setattr(dbos_app, "settings", SimpleNamespace(use_dbos=False))

    assert isinstance(dbos_app.launch_dbos(), DbosDisabled)


def test_a_missing_package_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dbos_app, "_dbos_launched", False)
    monkeypatch.setattr(dbos_app, "settings", SimpleNamespace(use_dbos=True))
    monkeypatch.setattr(dbos_app, "DBOS", None)

    outcome = dbos_app.launch_dbos()

    assert isinstance(outcome, DbosUnavailable)
    assert "not importable" in outcome.reason


def test_a_second_launch_reuses_the_running_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dbos_app, "_dbos_launched", False)
    monkeypatch.setattr(dbos_app, "settings", SimpleNamespace(use_dbos=True))
    monkeypatch.setattr(dbos_app, "DBOS", _QuietDBOS)
    monkeypatch.setattr(_QuietDBOS, "launches", 0)

    first = dbos_app.launch_dbos()
    second = dbos_app.launch_dbos()

    assert isinstance(first, DbosLaunched)
    assert isinstance(second, DbosLaunched)
    assert _QuietDBOS.launches == 1
    assert dbos_app.dbos_runtime_active() is True
