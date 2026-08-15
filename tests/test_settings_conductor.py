# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from local_first_agent_os._dbos_runtime import build_dbos_runtime_config
from local_first_agent_os.settings import Settings


def test_settings_accepts_dbos_conductor_environment(monkeypatch) -> None:
    monkeypatch.setenv("DBOS_APPLICATION_NAME", "registered-conductor-app")
    monkeypatch.setenv("DBOS__APPVERSION", "v-test")
    monkeypatch.setenv("DBOS_CONDUCTOR_KEY", "test-key")
    monkeypatch.setenv("DBOS_CONDUCTOR_URL", "wss://conductor.example.test")
    monkeypatch.setenv("DBOS_CONDUCTOR_EXECUTOR_METADATA", '{"pod":"local-agent-0"}')

    settings = Settings()

    assert settings.app_name == "registered-conductor-app"
    assert settings.application_version == "v-test"
    assert settings.dbos_conductor_key == "test-key"
    assert settings.dbos_conductor_url == "wss://conductor.example.test"
    assert settings.dbos_conductor_executor_metadata == {"pod": "local-agent-0"}
    assert build_dbos_runtime_config(settings).get("conductor_key") == "test-key"


def test_dbos_admin_server_is_disabled_by_default() -> None:
    config = build_dbos_runtime_config(Settings())

    assert config.get("run_admin_server") is False
