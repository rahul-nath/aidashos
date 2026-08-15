# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path

from local_first_agent_os.project_center import load_project_center
from local_first_agent_os.project_scaffold import (
    TargetProjectScaffold,
    scaffold_target_project,
)


def _registry(path: Path) -> None:
    path.write_text(
        """
[center]
id = "test"
description = "test center"
control_plane_project = "control"
default_saga_project = "control"
default_memory_project = "control"

[[projects]]
id = "control"
kind = "control_plane"
path = "/tmp/control"
status = "active"
read_only = false
description = "test control"
primary_interfaces = []
owns = []
avoid = []
verification_commands = ["true"]
""".lstrip(),
        encoding="utf-8",
    )


def test_scaffold_uses_uv_init_pins_toolchains_and_registers(
    runtime,
    tmp_path: Path,
    monkeypatch,
) -> None:
    projects_root = tmp_path / "projects"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    registry = config_dir / "linked_projects.toml"
    _registry(registry)
    settings = runtime.settings.model_copy(
        update={"projects_root": projects_root, "config_dir": config_dir}
    )
    spec = TargetProjectScaffold(
        project_id="public_repo_creator",
        path=str(projects_root / "public_repo_creator"),
    )
    finalized = tmp_path / "finalized.txt"
    finalized.write_text("# Approved GAWD\n", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, cwd: Path) -> None:
        commands.append(command)
        if command[:2] == ["uv", "init"]:
            (cwd / "pyproject.toml").write_text(
                '[project]\nname = "public-repo-creator"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
            (cwd / "README.md").write_text("# public-repo-creator\n", encoding="utf-8")
            (cwd / "main.py").write_text('print("hello")\n', encoding="utf-8")
        elif command[:3] == ["uv", "add", "--dev"]:
            (cwd / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    monkeypatch.setattr(
        "local_first_agent_os.project_scaffold._require_node_version",
        lambda _version: None,
    )
    monkeypatch.setattr(
        "local_first_agent_os.project_scaffold._run_checked_scaffold_command", fake_run
    )

    first = scaffold_target_project(spec, settings=settings, finalized_gawd_path=finalized)
    second = scaffold_target_project(spec, settings=settings, finalized_gawd_path=finalized)

    target = projects_root / "public_repo_creator"
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert any(command[:2] == ["uv", "init"] for command in commands)
    assert (target / ".python-version").read_text() == "3.13\n"
    assert (target / ".nvmrc").read_text() == "22.19.0\n"
    assert (target / ".node-version").read_text() == "22.19.0\n"
    assert json.loads((target / "package.json").read_text())["engines"] == {"node": ">=22.19.0 <23"}
    assert (target / "docs" / "design" / "gawd_doc.md").read_text() == ("# Approved GAWD\n")
    workflow = (target / ".github" / "workflows" / "ci.yml").read_text()
    assert "node-version-file: .nvmrc" in workflow
    assert "uv sync --locked" in workflow
    center = load_project_center(settings)
    registered = center.project_by_id("public_repo_creator")
    assert registered.expanded_path == target
    assert registered.verification_commands == [
        "uv run pytest",
        "uv run ruff check",
        "uv run pyright",
    ]
