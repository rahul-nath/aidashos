# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A fresh home can install dispatcher identity without downloads or implicit authority."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from local_first_agent_os.operator_credentials import (
    CredentialInitialization,
    initialize_operator_credential,
    read_host_operator_credential,
)
from local_first_agent_os.operator_identity import OPERATOR_TOKEN_ENV, OPERATOR_TOKEN_FILE_ENV

REPO_ROOT = Path(__file__).resolve().parents[1]


def _fresh_environment(home: Path) -> dict[str, str]:
    return {
        **{
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                OPERATOR_TOKEN_ENV,
                OPERATOR_TOKEN_FILE_ENV,
            }
        },
        "HOME": str(home),
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "UV_PROJECT_ENVIRONMENT": str(Path(sys.executable).parent.parent),
        "UV_CACHE_DIR": str(home / "uv-cache"),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_OFFLINE": "1",
    }


def _readiness(env: dict[str, str], tools: Path) -> subprocess.CompletedProcess[str]:
    tools.mkdir(exist_ok=True)
    shim = tools / "uv"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  "run --offline --no-sync python -m local_first_agent_os.operator_credentials check")\n'
        f"    exec {shlex.quote(sys.executable)} "
        "-m local_first_agent_os.operator_credentials check;;\n"
        '  "--version") echo "uv test-toolchain";;\n'
        "  *) exit 1;;\nesac\n"
    )
    shim.chmod(0o755)
    for name in ("curl", "docker", "codex", "claude"):
        executable = tools / name
        executable.write_text("#!/bin/sh\nexit 1\n")
        executable.chmod(0o755)
    return subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "scripts/first-run-check.sh")],
        env={**env, "PATH": f"{tools}:/usr/bin:/bin"},
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_fresh_installer_then_readiness_and_authenticated_dispatcher(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _fresh_environment(home)
    token = home / ".local-agent" / "operator.token"
    before = _readiness(env, tmp_path / "check-tools")
    assert "protected operator credential is missing or invalid" in before.stdout
    assert "./scripts/initialize-operator-identity.sh" in before.stdout
    assert not token.parent.exists()
    command = ["/bin/bash", str(REPO_ROOT / "scripts/initialize-operator-identity.sh")]
    first = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert first.returncode == 0, first.stdout + first.stderr
    credential = token.read_bytes()
    assert len(credential.strip()) >= 64
    assert token.stat().st_mode & 0o777 == 0o600
    assert credential.decode().strip() not in first.stdout + first.stderr
    second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "preserved" in second.stdout
    assert token.read_bytes() == credential
    assert (
        "protected operator credential is ready" in _readiness(env, tmp_path / "check-tools").stdout
    )
    authenticated = subprocess.run(
        [
            sys.executable,
            "-c",
            "from local_first_agent_os.operator_dispatcher_host import provision_dispatcher_host\n"
            "provision_dispatcher_host()\n",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert authenticated.returncode == 0, authenticated.stderr
    assert not authenticated.stdout


@pytest.mark.parametrize("invalid", ["empty", "exposed", "symlink", "directory", "fifo"])
def test_initialization_never_replaces_an_invalid_existing_identity(
    tmp_path: Path, invalid: str
) -> None:
    token = tmp_path / "operator.token"
    if invalid == "symlink":
        target = tmp_path / "target"
        target.write_text("retained-secret")
        target.chmod(0o600)
        token.symlink_to(target)
    elif invalid == "directory":
        token.mkdir()
    elif invalid == "fifo":
        os.mkfifo(token, 0o600)
    else:
        token.write_text("" if invalid == "empty" else "retained-secret")
        token.chmod(0o644 if invalid == "exposed" else 0o600)
    before = token.lstat()
    with pytest.raises((OSError, PermissionError)):
        initialize_operator_credential(token)
    after = token.lstat()
    assert (after.st_ino, after.st_mode, after.st_size) == (
        before.st_ino,
        before.st_mode,
        before.st_size,
    )


def test_installation_preserves_a_valid_configured_identity(tmp_path: Path) -> None:
    token = tmp_path / "operator.token"
    assert initialize_operator_credential(token) is CredentialInitialization.CREATED
    credential = read_host_operator_credential(token)
    assert initialize_operator_credential(token) is CredentialInitialization.PRESERVED
    assert read_host_operator_credential(token) == credential


def test_both_installers_use_the_same_explicit_identity_step() -> None:
    for name in ("bootstrap.sh", "install_pi_shell.sh"):
        source = (REPO_ROOT / "scripts" / name).read_text()
        assert '"$ROOT/scripts/initialize-operator-identity.sh"' in source
