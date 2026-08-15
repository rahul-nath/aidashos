# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A missing target project is adopted, not refused.

The operator's standing rule: no milestone is ever blocked because a target
directory was left out. If none exists, make one and put the work there.

`adopt_unregistered_target` is the compile-time half of `project_scaffold`,
which already owned scaffolding. The approval-time half runs `uv init`, adds dev
dependencies, projects verification into CI, and needs a finalized GAWD
document; none of that can happen inside a synchronous compile, and neither can
a test suite that needs the network. What both halves share is a real git
repository at a known path and a registry entry, and the shared half is what
this reuses rather than restates.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from local_first_agent_os.project_scaffold import adopt_unregistered_target
from local_first_agent_os.settings import Settings

_REGISTRY = """
[center]
id = "test_center"
description = "test"
control_plane_project = "existing_project"
default_saga_project = "existing_project"
default_memory_project = "existing_project"

[[projects]]
id = "existing_project"
kind = "control_plane"
path = "~/somewhere"
status = "active_center"
read_only = false
description = "already registered"
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "configs"
    directory.mkdir()
    (directory / "linked_projects.toml").write_text(_REGISTRY, encoding="utf-8")
    return directory


@pytest.fixture
def registry(config_dir: Path) -> Path:
    return config_dir / "linked_projects.toml"


@pytest.fixture
def settings(config_dir: Path) -> Settings:
    # `config_dir`, not `linked_projects_path`: the latter is a derived property,
    # and `Settings` ignores unknown keyword arguments, so passing it looks like
    # it works while leaving the real operator registry in place. It did, once.
    return Settings(config_dir=config_dir)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    directory = tmp_path / "work"
    directory.mkdir()
    return directory


def test_creates_a_git_repository_and_registers_it(
    settings: Settings, registry: Path, work: Path
) -> None:
    result = adopt_unregistered_target("brand_new_thing", settings=settings, root=work)

    assert result["created"] is True
    target = Path(result["path"])
    assert target == work / "brand_new_thing"
    assert (target / "README.md").is_file()
    # Worktrees, checkpoints, and the verification diff all assume a repository.
    assert (target / ".git").exists()
    branch = subprocess.run(
        ["git", "-C", str(target), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert branch.stdout.strip() == "main"

    registered = tomllib.loads(registry.read_text(encoding="utf-8"))
    ids = {project["id"] for project in registered["projects"]}
    assert ids == {"existing_project", "brand_new_thing"}


def test_the_existing_registry_comments_survive(
    settings: Settings, registry: Path, work: Path
) -> None:
    """Appended as text by the module's own writer, so comments are not lost."""

    registry.write_text(
        registry.read_text(encoding="utf-8") + "\n# a comment worth keeping\n",
        encoding="utf-8",
    )

    adopt_unregistered_target("another_one", settings=settings, root=work)

    contents = registry.read_text(encoding="utf-8")
    assert "# a comment worth keeping" in contents
    assert 'id = "another_one"' in contents


def test_adopting_twice_is_idempotent(settings: Settings, registry: Path, work: Path) -> None:
    adopt_unregistered_target("twice", settings=settings, root=work)
    second = adopt_unregistered_target("twice", settings=settings, root=work)

    assert second["created"] is False
    assert registry.read_text(encoding="utf-8").count('id = "twice"') == 1


def test_a_registered_id_pointing_elsewhere_is_refused(
    settings: Settings, registry: Path, work: Path, tmp_path: Path
) -> None:
    """The module's own guard: an id is a name for one directory, not any."""

    adopt_unregistered_target("claimed", settings=settings, root=work)
    other = tmp_path / "elsewhere"
    other.mkdir()

    with pytest.raises(ValueError, match="already points elsewhere"):
        adopt_unregistered_target("claimed", settings=settings, root=other)


@pytest.mark.parametrize(
    "project_id", ["../escape", "has space", "Upper", "/absolute", "", "9leading", "with-dash"]
)
def test_ids_that_cannot_become_a_directory_are_refused(
    project_id: str, settings: Settings, work: Path
) -> None:
    """The one thing that still fails, because it is a typo the operator must see.

    The rule is `project_scaffold`'s own `_PROJECT_ID`, not a second opinion:
    lowercase start, then lowercase letters, digits, and underscores.
    """

    with pytest.raises(ValueError):
        adopt_unregistered_target(project_id, settings=settings, root=work)

    assert list(work.iterdir()) == []
