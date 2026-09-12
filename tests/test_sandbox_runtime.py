# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_first_agent_os import sandbox_runtime
from local_first_agent_os.process_containment import ProcessContainmentUnavailable
from local_first_agent_os.sandbox_runtime import (
    ReadOnlyToolWorker,
    SandboxRuntimeInstallation,
    _run_identity,
    _runtime_tree_sha256,
)


@pytest.fixture
def prepared_worker_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake-installation policy tests prepare files without opening native SRT sockets."""

    def owned_directory(*, prefix: str, dir: str):
        return tempfile.TemporaryDirectory(prefix=prefix, dir=tmp_path)

    monkeypatch.setattr(
        sandbox_runtime, "tempfile", SimpleNamespace(TemporaryDirectory=owned_directory)
    )


def test_identity_probe_cannot_inherit_preload_or_git_overrides(monkeypatch) -> None:
    for key in ("NODE_OPTIONS", "PYTHONPATH", "GIT_CONFIG_COUNT", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "fixture-only-no-ambient-authority")
    assert (
        _run_identity(
            (
                sys.executable,
                "-c",
                "import os; print(any(key in os.environ for key in "
                "('NODE_OPTIONS', 'PYTHONPATH', 'GIT_CONFIG_COUNT', 'OPENAI_API_KEY')))",
            )
        )
        == "False"
    )


@pytest.mark.parametrize("link_kind", ["directory", "foreign_file", "missing"])
def test_runtime_identity_refuses_unmeasured_symlinks(tmp_path, link_kind) -> None:
    source = tmp_path / "source"
    (source / "dist").mkdir(parents=True)
    modules = source / "node_modules"
    modules.mkdir()
    external = tmp_path / "external"
    if link_kind == "directory":
        external.mkdir()
    elif link_kind == "foreign_file":
        external.write_text("fixture foreign code")
    (modules / "dependency").symlink_to(external)
    with pytest.raises((ValueError, FileNotFoundError)):
        _runtime_tree_sha256(source)


def test_runtime_identity_measures_internal_file_symlink_target(tmp_path) -> None:
    (tmp_path / "dist").mkdir()
    modules = tmp_path / "node_modules"
    modules.mkdir()
    one, two = modules / "one", modules / "two"
    one.write_text("same bytes")
    two.write_text("same bytes")
    link = modules / "executable"
    link.symlink_to(one)
    before = _runtime_tree_sha256(tmp_path)
    link.unlink()
    link.symlink_to(two)
    assert _runtime_tree_sha256(tmp_path) != before


def test_worker_has_private_state_and_no_ambient_authority(
    prepared_worker_state, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCAL_AGENT_OPERATOR_TOKEN", "operator-canary")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-canary")
    monkeypatch.setenv("LOCAL_AGENT_COORDINATION_DATABASE_URL", "writer-canary")
    monkeypatch.setattr(SandboxRuntimeInstallation, "revalidate", lambda _: None)
    installation = SandboxRuntimeInstallation(tmp_path, Path("/bin/sh"), "fixture")
    worker = ReadOnlyToolWorker(installation, tmp_path)
    with worker.contain_service(("/bin/cat",)) as contained:
        request = json.loads(Path(contained.command[-1]).read_text())
        assert not any("canary" in value for value in contained.environment.values())
        assert Path(contained.environment["HOME"]) == contained.scratch_path
        assert Path(contained.environment["CLAUDE_CODE_TMPDIR"]) == contained.scratch_path
        assert request["config"]["network"] == {
            "allowedDomains": [],
            "deniedDomains": ["*"],
            "strictAllowlist": True,
            "allowLocalBinding": False,
            "allowAllUnixSockets": False,
        }
        assert request["config"]["filesystem"]["allowWrite"] == [str(contained.scratch_path)]
        assert set(request["config"]["filesystem"]["denyWrite"]) == {
            str(tmp_path),
            "/tmp/claude",
            "/private/tmp/claude",
            "/dev/tty",
            "/dev/dtracehelper",
            "/dev/autofs_nowait",
        }
        assert contained.identity.runtime_sha256 == "fixture"
        assert (
            contained.identity.request_sha256
            == hashlib.sha256(Path(contained.command[-1]).read_bytes()).hexdigest()
        )
        assert (
            contained.identity.policy_sha256
            == hashlib.sha256(
                json.dumps(request["config"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        scratch = contained.scratch_path
    assert not scratch.exists()


def test_installation_drift_prevents_any_launch(tmp_path, monkeypatch) -> None:
    installation = SandboxRuntimeInstallation(tmp_path, Path("/bin/sh"), "old")
    monkeypatch.setattr(
        SandboxRuntimeInstallation,
        "inspect",
        classmethod(lambda cls, source, node: SandboxRuntimeInstallation(source, node, "changed")),
    )
    with (
        pytest.raises(ProcessContainmentUnavailable, match="changed"),
        ReadOnlyToolWorker(installation, tmp_path).contain_service(("/bin/cat",)),
    ):
        pytest.fail("installation drift must fail before yielding a command")


@pytest.mark.parametrize("changed", ["request", "service"])
def test_bridge_refuses_prepared_launch_drift(
    prepared_worker_state, tmp_path, monkeypatch, changed
) -> None:
    node = os.environ.get("LOCAL_AGENT_SRT_PROBE_NODE")
    if not node:
        pytest.skip("explicit existing Node toolchain not configured")
    monkeypatch.setattr(SandboxRuntimeInstallation, "revalidate", lambda _: None)
    installation = SandboxRuntimeInstallation(tmp_path, Path(node), "fixture")
    executable = tmp_path / "service"
    executable.write_text("fixture executable before preparation")
    with ReadOnlyToolWorker(installation, tmp_path).contain_service((str(executable),)) as prepared:
        if changed == "request":
            request = Path(prepared.command[-1])
            request.write_text(request.read_text() + " ")
        else:
            executable.write_text("fixture executable changed after preparation")
        result = subprocess.run(
            prepared.command,
            env=prepared.environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    assert result.returncode != 0
    expected = (
        "Tool-worker launch request changed"
        if changed == "request"
        else "Tool-worker service executable changed"
    )
    assert expected in result.stderr
    assert "Cannot find module" not in result.stderr
