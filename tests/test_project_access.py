# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One access decision per project, instead of a boolean and two lists."""

from __future__ import annotations

from pathlib import Path

from test_project_center import write_registry

from local_first_agent_os.project_access import (
    AccessMode,
    ProjectAccessPolicy,
    access_policy_from_record,
)
from local_first_agent_os.project_center import load_project_center
from local_first_agent_os.settings import Settings


def test_read_only_is_derived_so_it_cannot_disagree_with_the_mode() -> None:
    assert access_policy_from_record(read_only=True, owns=[], avoid=[]).read_only is True
    assert access_policy_from_record(read_only=False, owns=[], avoid=[]).read_only is False


def test_a_registry_preserves_read_only_and_writable_access(tmp_path: Path) -> None:
    """`dispatcher_runner` and `decomposition` read `project.read_only`.

    They now read a mode through that name. The registry must keep parsing and
    the property must keep answering, because a code dispatch to a read-only
    project is refused on the strength of it.
    """

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    write_registry(config_dir, tmp_path / "project")
    registry = config_dir / "linked_projects.toml"
    records = registry.read_text().rsplit("read_only = false", 1)
    registry.write_text("read_only = true".join(records))
    center = load_project_center(Settings(config_dir=config_dir))
    read_only = [project.id for project in center.projects if project.read_only]
    assert read_only == ["ai_stack_local"]
    assert any(not project.read_only for project in center.projects)
    for project in center.projects:
        assert project.read_only is project.access.read_only
        assert project.owns == list(project.access.owns)


def test_remit_is_prose_and_is_never_matched_against_paths() -> None:
    """The registry spells `owns` and `avoid` as sentences, not globs.

    Enforcing them as path patterns made every changed file look out of bounds,
    because "voice and terminal command interface" matches no filename. The type
    carries them so an agent can read them; nothing here compares them to a path.
    """

    policy = ProjectAccessPolicy(
        mode=AccessMode.READ_WRITE,
        owns=("coordination ledger", "agent orchestration policy"),
        avoid=("raw personal-memory exports",),
    )
    assert not hasattr(policy, "may_write")
    assert not hasattr(policy, "refusals_for")
    assert "coordination ledger" in policy.owns
