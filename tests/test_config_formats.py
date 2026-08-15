# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

from local_first_agent_os.constants import (
    DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_SAGA_TASK_TIMEOUT_SECONDS,
)
from local_first_agent_os.settings import Settings


def test_application_config_directory_is_toml_only() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    config_dir = repo_root / "configs"
    config_files = [path for path in config_dir.iterdir() if path.is_file()]

    assert config_files
    assert all(path.suffix == ".toml" for path in config_files)


def test_runtime_registry_paths_use_toml() -> None:
    settings = Settings(config_dir=Path("configs"), mock_models=True)

    assert settings.model_registry_path.suffix == ".toml"
    assert settings.workspace_policy_path.suffix == ".toml"
    assert settings.directive_config_path.suffix == ".toml"
    assert settings.pi_prompts_path.suffix == ".toml"
    assert settings.linked_projects_path.suffix == ".toml"


def test_saga_executor_env_alias_selects_fake_process(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_AGENT_SAGA_EXECUTOR", "fake_process")

    settings = Settings(config_dir=Path("configs"), mock_models=True)

    assert settings.saga_executor_backend == "fake_process"


def test_the_process_cap_outlasts_the_longest_milestone_budget() -> None:
    """It used to be one hour, and one hour was shorter than the work.

    This shared `DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS` with the model-call budget,
    which reads sensible and was not: `implement.code_change` declares 5400s for
    itself, the supervisor's clock and the milestone's clock race, and the
    supervisor's always won. The implement milestone was killed at sixty minutes
    and parked BLOCKED with `dispatch_paused`, so its declared ninety could never
    be reached - a bound that cannot fire.

    Two constants now, because they answer different questions: how long one
    model call may take, and how long one dispatched agent process may run.
    """

    settings = Settings(config_dir=Path("configs"), mock_models=True)

    assert settings.saga_task_timeout_seconds == DEFAULT_SAGA_TASK_TIMEOUT_SECONDS
    assert settings.saga_task_timeout_seconds > DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
