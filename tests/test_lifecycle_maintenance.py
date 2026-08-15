# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from local_first_agent_os.lifecycle_maintenance import (
    bound_log_files,
    run_lifecycle_maintenance,
)
from local_first_agent_os.runtime import AppRuntime
from local_first_agent_os.session_memory import _wait_for_session_runtime
from local_first_agent_os.settings import Settings


def test_bound_log_files_retains_tail_in_same_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log = log_dir / "daemon.err.log"
    log.write_bytes(b"0123456789")

    result = bound_log_files(log_dir, max_bytes=8, retained_tail_bytes=4)

    assert log.read_bytes() == b"6789"
    assert result == [
        {
            "path": str(log),
            "original_bytes": 10,
            "retained_bytes": 4,
            "reclaimed_bytes": 6,
        }
    ]


def _maintenance_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings.model_validate(
        {
            "lifecycle_log_dir": tmp_path / "logs",
            "lifecycle_maintenance_state_path": tmp_path / "state" / "latest.json",
            "lifecycle_log_max_bytes": 8,
            "lifecycle_log_retained_tail_bytes": 4,
            **overrides,
        }
    )


def test_scheduled_maintenance_applies_the_configured_retention_window(
    tmp_path: Path,
) -> None:
    """The window must reach ``gc_ledger``, or retention is dead configuration.

    This is the regression the module was built around: ``gc_ledger`` grew
    retention support that the scheduled caller never passed, so the capability
    existed and no run ever exercised it.
    """

    settings = _maintenance_settings(tmp_path, lifecycle_retention_seconds=1234)
    seen: dict[str, object] = {}

    def recording_gc(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"ok": True, "deleted": {}}

    report = run_lifecycle_maintenance(settings, gc=recording_gc)

    assert seen == {"retention_seconds": 1234}
    assert report["retention_seconds"] == 1234
    assert report["status"] == "COMPLETED"


def test_retention_can_be_turned_off_without_disabling_maintenance(
    tmp_path: Path,
) -> None:
    settings = _maintenance_settings(tmp_path, lifecycle_retention_seconds=None)
    seen: dict[str, object] = {}

    def recording_gc(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"ok": True, "deleted": {}}

    report = run_lifecycle_maintenance(settings, gc=recording_gc)

    assert seen == {"retention_seconds": None}
    assert report["retention_seconds"] is None
    assert report["status"] == "COMPLETED"


def test_lifecycle_maintenance_records_one_bounded_degraded_state(tmp_path: Path) -> None:
    settings = _maintenance_settings(tmp_path)

    def unavailable_gc(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("database URL and credentials must not be persisted")

    report = run_lifecycle_maintenance(settings, gc=unavailable_gc)

    assert report["status"] == "DEGRADED"
    assert report["ledger"] == {
        "ok": False,
        "error_type": "RuntimeError",
        "message": "coordination ledger unavailable; retry scheduled",
    }
    assert (
        settings.lifecycle_maintenance_state_path.read_text(encoding="utf-8").count(
            "coordination ledger unavailable"
        )
        == 1
    )
    assert "credentials" not in settings.lifecycle_maintenance_state_path.read_text(
        encoding="utf-8"
    )


def test_session_runtime_wait_logs_one_outage_and_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected = cast(AppRuntime, object())
    attempts = 0
    delays: list[float] = []

    def runtime_factory() -> AppRuntime:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OperationalError("connect", {}, ConnectionRefusedError())
        return expected

    result = _wait_for_session_runtime(
        runtime_factory,
        delays.append,
        initial_retry_seconds=1,
        max_retry_seconds=10,
    )

    assert result is expected
    assert delays == [1, 2]
    assert caplog.text.count("session_daemon_database_unavailable") == 1
    assert caplog.text.count("session_daemon_database_recovered") == 1


def test_a_zero_retention_window_is_rejected() -> None:
    """Zero would mean "keep nothing", which is deletion wearing a config's clothes.

    ``None`` is how retention is turned off. The lower bound is what keeps a
    fat-fingered 0 from reading as "sweep everything on the next scheduled run".
    """

    with pytest.raises(ValidationError):
        Settings.model_validate({"lifecycle_retention_seconds": 0})

    with pytest.raises(ValidationError):
        Settings.model_validate({"lifecycle_retention_seconds": -1})


def test_the_documented_environment_variable_reaches_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """.env.example promises this name; nothing else checks that it binds.

    A documented-but-unbound variable is the quiet kind of wrong: the operator
    sets it, no error appears, and the default silently stays in force.
    """

    monkeypatch.setenv("LOCAL_AGENT_LIFECYCLE_RETENTION_SECONDS", "4321")

    # _env_file=None so a developer's real .env cannot decide this result.
    assert Settings(_env_file=None).lifecycle_retention_seconds == 4321  # type: ignore[call-arg]


def test_the_default_retention_window_is_ninety_days() -> None:
    """Pins the number .env.example documents, so the two cannot drift apart."""

    assert Settings(_env_file=None).lifecycle_retention_seconds == 7776000  # type: ignore[call-arg]
