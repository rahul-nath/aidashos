# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check physical NVM layout and fail-closed copying without downloading Node."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_first_agent_os.toolchains import installed_node_environment, project_environment
from scripts.ci.prepare_node import prepare


@pytest.fixture
def installation(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".nvmrc").write_text("22.19.0\n")
    source = tmp_path / "hostedtoolcache" / "node" / "22.19.0" / "arm64"
    executable = source / "bin" / "node"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\necho v22.19.0\n")
    executable.chmod(0o755)
    module = source / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    module.parent.mkdir(parents=True)
    module.write_text("// npm fixture\n")
    (source / "bin" / "npm").symlink_to("../lib/node_modules/npm/bin/npm-cli.js")
    return root, executable, tmp_path / "ci-private-nvm"


def test_copy_is_physical_and_satisfies_runtime_selection(
    installation: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, source, nvm = installation
    copied = prepare(root, source, nvm)
    assert copied == nvm / "versions/node/v22.19.0/bin/node"
    assert copied.resolve() == copied
    assert copied.read_bytes() == source.read_bytes()
    assert (copied.parent / "npm").resolve().is_relative_to(nvm)
    monkeypatch.setenv("NVM_DIR", str(nvm))
    assert project_environment(root)["PATH"].split(":")[0] == str(copied.parent)
    assert installed_node_environment(root, copied) == {"NVM_DIR": str(nvm)}


def test_wrong_version_cannot_publish_a_distribution(
    installation: tuple[Path, Path, Path],
) -> None:
    root, source, nvm = installation
    (root / ".nvmrc").write_text("24.0.0")
    with pytest.raises(ValueError, match="does not match"):
        prepare(root, source, nvm)
    assert not nvm.exists()


@pytest.mark.parametrize("version", ("lts/*", "../../22.19.0", "22", "22.19.0\n24.0.0"))
def test_nonexact_version_cannot_choose_a_path(
    installation: tuple[Path, Path, Path], version: str
) -> None:
    root, source, nvm = installation
    (root / ".nvmrc").write_text(version)
    with pytest.raises(ValueError, match="exact Node version"):
        prepare(root, source, nvm)
    assert not nvm.exists()


def test_existing_destination_is_preserved(installation: tuple[Path, Path, Path]) -> None:
    root, source, nvm = installation
    copied = prepare(root, source, nvm)
    with pytest.raises(ValueError, match="must be fresh"):
        prepare(root, source, nvm)
    assert copied.read_bytes() == source.read_bytes()


def test_external_distribution_symlink_is_refused(
    installation: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    root, source, nvm = installation
    foreign = tmp_path / "foreign"
    foreign.write_text("private")
    (source.parent / "external").symlink_to(foreign)
    with pytest.raises(ValueError, match="outside its copied layout"):
        prepare(root, source, nvm)
    assert not nvm.exists()
