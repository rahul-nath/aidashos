# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Published examples must admit against a registry with no private aliases."""

from pathlib import Path

import pytest
from work_unit_support import (
    ACCEPTANCE_DESIGN_DOC,
    acceptance_target_project_id,
    write_test_project_registry,
)

from local_first_agent_os.project_center import load_project_center
from local_first_agent_os.settings import get_settings
from local_first_agent_os.work_units.compiler import CompiledPlanOutcome, compile_design_doc
from local_first_agent_os.work_units.design_doc import parse_design_doc

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("example", ["acceptance", "golden_path"])
def test_public_example_names_the_registered_checkout_without_adoption(
    example: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "renamed-install"
    config_dir = checkout / "configs"
    registry = write_test_project_registry(config_dir, "local_first_agent_os", Path("."))
    registry_before = registry.read_bytes()
    monkeypatch.setenv("LOCAL_AGENT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LOCAL_AGENT_PROJECTS_ROOT", str(tmp_path / "unregistered"))
    elsewhere = tmp_path / "another-working-directory"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    get_settings.cache_clear()
    document = (REPO_ROOT / "docs" / "examples" / f"work_unit_{example}_design_doc.md").read_text(
        encoding="utf-8"
    )

    outcome = compile_design_doc(
        parse_design_doc(document, design_doc_id=f"public_{example}"),
        design_doc_revision_id=f"public_{example}_revision",
    )

    assert isinstance(outcome, CompiledPlanOutcome)
    assert outcome.runnable, outcome.execution_blockers
    assert outcome.plan.target_project_id == "local_first_agent_os"
    project = load_project_center().project_by_id(outcome.plan.target_project_id)
    assert project.expanded_path == checkout
    assert registry.read_bytes() == registry_before
    assert not (tmp_path / "unregistered").exists()


def test_default_test_compilation_cannot_adopt_into_the_operator_checkout() -> None:
    operator_registry = REPO_ROOT / "configs" / "linked_projects.toml"
    operator_registry_before = operator_registry.read_bytes()
    settings = get_settings()
    assert settings.linked_projects_path != operator_registry
    target_id = "portable_adopted_project"
    document = ACCEPTANCE_DESIGN_DOC.replace(
        f"Target project: {acceptance_target_project_id()}", f"Target project: {target_id}"
    )

    outcome = compile_design_doc(
        parse_design_doc(document, design_doc_id="isolated_adoption"),
        design_doc_revision_id="isolated_adoption_revision",
    )

    assert isinstance(outcome, CompiledPlanOutcome)
    assert outcome.runnable, outcome.execution_blockers
    project = load_project_center().project_by_id(target_id)
    assert project.expanded_path == settings.projects_root / target_id
    assert (project.expanded_path / ".git").is_dir()
    assert operator_registry.read_bytes() == operator_registry_before
