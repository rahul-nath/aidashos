# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from local_first_agent_os.project_center import load_project_center
from local_first_agent_os.settings import Settings


def write_registry(config_dir: Path, project_path: Path) -> None:
    (config_dir / "linked_projects.toml").write_text(
        f"""
[center]
id = "local-first-agent-os"
description = "test center"
control_plane_project = "local-first-agent-os"
default_saga_project = "ai_business_portfolio"
default_memory_project = "ai_stack_local"

[[projects]]
id = "local-first-agent-os"
kind = "control_plane"
path = "{project_path}"
status = "active_center"
read_only = false
description = "control plane"
primary_interfaces = ["pi"]
owns = ["commands"]
avoid = ["products"]
verification_commands = ["uv run pytest"]

[[projects]]
id = "ai_business_portfolio"
kind = "business_factory"
path = "{project_path}"
status = "active_product_repo"
read_only = false
description = "business factory"
primary_interfaces = ["pnpm check"]
owns = ["products"]
avoid = ["memory"]
verification_commands = ["pnpm check"]

[[projects]]
id = "ai_stack_local"
kind = "personal_memory"
path = "{project_path}"
status = "active_memory_repo"
read_only = false
description = "personal memory"
primary_interfaces = ["uv run embed.py"]
owns = ["memory"]
avoid = ["products"]
verification_commands = ["uv run python -m py_compile embed.py"]
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_load_project_center(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    project_path = tmp_path / "project"
    project_path.mkdir()
    write_registry(config_dir, project_path)

    settings = Settings(config_dir=config_dir, mock_models=True)
    center = load_project_center(settings)

    assert center.id == "local-first-agent-os"
    assert center.control_plane_project == "local-first-agent-os"
    assert center.default_saga_project == "ai_business_portfolio"
    assert center.default_saga_target().id == "ai_business_portfolio"
    assert center.default_memory_project == "ai_stack_local"
    assert center.project_by_id("ai_stack_local").kind == "personal_memory"

    rows = center.status_rows(include_git=False)
    assert len(rows) == 3
    assert {row["id"] for row in rows} == {
        "local-first-agent-os",
        "ai_business_portfolio",
        "ai_stack_local",
    }
    assert all(row["exists"] for row in rows)


def test_project_center_rejects_unknown_center_reference(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    project_path = tmp_path / "project"
    project_path.mkdir()
    write_registry(config_dir, project_path)
    registry = config_dir / "linked_projects.toml"
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            'default_saga_project = "ai_business_portfolio"',
            'default_saga_project = "missing_project"',
        ),
        encoding="utf-8",
    )

    settings = Settings(config_dir=config_dir, mock_models=True)
    with pytest.raises(ValueError, match="unknown project"):
        load_project_center(settings)
