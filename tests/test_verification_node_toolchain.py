# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The committed Node pin survives host selection and isolated gate environments."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from local_first_agent_os import host_verification as host
from local_first_agent_os.toolchains import installed_node_environment, project_environment


def _node(nvm: Path, version: str) -> Path:
    node = nvm / "versions" / "node" / f"v{version}" / "bin" / "node"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_bytes(b"fixture Node, never executed")
    node.chmod(0o755)
    return node


@pytest.fixture
def discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project, snapshot = tmp_path / "project", tmp_path / "snapshot"
    (project / ".venv").mkdir(parents=True)
    snapshot.mkdir()
    nvm = tmp_path / "nvm"
    pinned = _node(nvm, "22.19.0")
    ambient = _node(nvm, "26.8.1")
    tools = tmp_path / "tools"
    tools.mkdir()
    helpers = tmp_path / "libexec" / "git-core"
    helpers.mkdir(parents=True)
    for name in ("uv", "git"):
        executable = tools / name
        executable.write_bytes(b"fixture tool, never executed")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(ambient.parent), str(tools))))
    monkeypatch.setenv("NVM_DIR", str(nvm))
    monkeypatch.setattr(host, "_linked_runtime_files", lambda path: (path,))
    invocations = []

    def run(command, **kwargs):
        invocations.append(command)
        assert command == (str(tools / "git"), "--exec-path")
        return subprocess.CompletedProcess(command, 0, stdout=str(helpers), stderr="")

    monkeypatch.setattr(host.subprocess, "run", run)
    return project, snapshot, pinned, ambient, invocations


def test_discovery_selects_frozen_pin_before_ambient_node_without_executing_it(discovery):
    project, snapshot, pinned, _ambient, invocations = discovery
    (project / ".nvmrc").write_text("26.8.1\n")
    (snapshot / ".nvmrc").write_text("v22.19.0\n")
    toolchain = host._installed_toolchain(project, source_root=snapshot)
    assert toolchain.executables[2] == pinned
    assert len(invocations) == 1  # Only the existing protected Git helper lookup.


def test_discovery_without_a_frozen_pin_does_not_borrow_mutable_checkout_pin(discovery):
    project, snapshot, _pinned, ambient, _invocations = discovery
    (project / ".nvmrc").write_text("22.19.0\n")
    assert host._installed_toolchain(project, source_root=snapshot).executables[2] == ambient


@pytest.mark.parametrize("version", ["20.0.0", "node", "lts/*", "../../22.19.0"])
def test_missing_or_nonexact_frozen_pin_refuses_before_tool_execution(discovery, version):
    project, snapshot, _pinned, _ambient, invocations = discovery
    (snapshot / ".nvmrc").write_text(version)
    with pytest.raises(RuntimeError, match="not installed|exact Node version"):
        host._installed_toolchain(project, source_root=snapshot)
    assert invocations == []


def test_unselectable_pin_cannot_fall_through_to_ambient_node(discovery, monkeypatch):
    project, snapshot, pinned, ambient, _invocations = discovery
    (snapshot / ".nvmrc").write_text("22.19.0")
    find_executable = host.shutil.which

    def refuse_pinned(name, *, path=None):
        if name == "node":
            assert isinstance(path, str)
            assert Path(path.split(os.pathsep)[0]) == pinned.parent
            return str(ambient)
        return find_executable(name, path=path)

    # Resolver eligibility is the boundary: mode bits alone do not revoke an
    # inherited ACL's execute grant in a UID-owned verification fixture.
    monkeypatch.setattr(host.shutil, "which", refuse_pinned)
    with pytest.raises(RuntimeError, match="does not retain the NVM installation"):
        host._installed_toolchain(project, source_root=snapshot)


def test_gate_projection_uses_only_selected_installation_and_fails_on_pin_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    snapshot, output = tmp_path / "snapshot", tmp_path / "isolated-home"
    snapshot.mkdir()
    output.mkdir()
    (snapshot / ".nvmrc").write_text("22.19.0\n")
    staged_nvm = tmp_path / "staged" / "native" / "nvm"
    node = _node(staged_nvm, "22.19.0")
    monkeypatch.setenv("NVM_DIR", "/private/host-nvm-must-not-be-used")
    monkeypatch.setenv("LOCAL_AGENT_OPERATOR_TOKEN", "must-not-be-projected")
    toolchain = host._InstalledToolchain(
        tmp_path / "environment", (tmp_path / "uv", tmp_path / "git", node), ()
    )
    gate = toolchain.gate_environment(snapshot, output)
    assert gate["NVM_DIR"] == str(staged_nvm)
    assert gate["HOME"] == str(output)
    assert "LOCAL_AGENT_OPERATOR_TOKEN" not in gate
    nested = project_environment(snapshot, gate)
    assert Path(nested["PATH"].split(os.pathsep)[0]) / "node" == node
    (snapshot / ".nvmrc").write_text("26.8.1\n")
    with pytest.raises(RuntimeError, match="does not retain the NVM installation"):
        toolchain.gate_environment(snapshot, output)


def test_non_nvm_executable_cannot_claim_a_project_pin(tmp_path: Path):
    (tmp_path / ".nvmrc").write_text("22.19.0")
    node = tmp_path / "node"
    node.touch()
    with pytest.raises(RuntimeError, match="does not retain the NVM installation"):
        installed_node_environment(tmp_path, node)
