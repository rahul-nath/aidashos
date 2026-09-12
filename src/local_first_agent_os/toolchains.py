# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-project toolchain resolution for external agent worktrees."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from .operator_identity import OPERATOR_TOKEN_ENV, OPERATOR_TOKEN_FILE_ENV

_EXACT_NODE_VERSION = re.compile(r"(?:v)?(\d+\.\d+\.\d+)")

# Every variable this control plane sets to configure itself lives under one of
# these prefixes. Verification belongs to the target project, so these values
# must never change what its gate proves.
CONTROL_PLANE_ENV_PREFIXES: Final = ("LOCAL_AGENT_", "AGENT_COORDINATION_", "DBOS_")
CONTROL_PLANE_ENV_NAMES: Final = frozenset({"VIRTUAL_ENV", "AGENT_SESSION_ID"})


def unprivileged_process_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Child commands cannot inherit the host credential for operator mutations.

    Trusted coordination transports have their own authentication boundary and
    do not use this function. Explicit child overrides cannot restore the secret.
    """
    return {
        key: value
        for key, value in environment.items()
        if key not in {OPERATOR_TOKEN_ENV, OPERATOR_TOKEN_FILE_ENV}
    }


def _project_node_version(project_path: Path) -> str | None:
    version_file = project_path / ".nvmrc"
    if not version_file.is_file():
        return None
    raw = version_file.read_text(encoding="utf-8").strip()
    match = _EXACT_NODE_VERSION.fullmatch(raw)
    if match is None:
        raise RuntimeError(f"{version_file} must contain an exact Node version.")
    return match.group(1)


def _nvm_node_path(version: str) -> Path:
    return Path("versions") / "node" / f"v{version}" / "bin" / "node"


def installed_node_environment(project_path: Path, executable: Path) -> dict[str, str]:
    """Project the selected exact NVM installation after layout-preserving relocation.

    The host has already selected and copied these bytes. This only binds NVM's
    lookup root to that same installation; it never probes or selects another Node.
    """
    version = _project_node_version(project_path)
    if version is None:
        return {}
    suffix = _nvm_node_path(version)
    if (
        not executable.is_absolute()
        or executable.parts[-len(suffix.parts) :] != suffix.parts
        or not executable.is_file()
    ):
        raise RuntimeError(
            "Selected Node does not retain the NVM installation pinned by "
            f"{project_path / '.nvmrc'}."
        )
    return {"NVM_DIR": str(executable.parents[len(suffix.parts) - 1])}


def project_environment(
    project_path: Path,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment honoring an exact ``.nvmrc`` when present."""

    env = {**os.environ, **(overrides or {})}
    version = _project_node_version(project_path)
    if version is None:
        return env
    nvm_dir = Path(env.get("NVM_DIR") or (Path.home() / ".nvm")).expanduser()
    node = nvm_dir / _nvm_node_path(version)
    if not node.is_file():
        raise RuntimeError(
            f"Node {version} pinned by {project_path / '.nvmrc'} is not installed. "
            f"Run `source ~/.nvm/nvm.sh && nvm install {version}`."
        )
    path_parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    env["PATH"] = os.pathsep.join((str(node.parent), *path_parts))
    env["LOCAL_AGENT_NODE_VERSION"] = version
    return env


def verification_gate_environment(project_path: Path) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return the target toolchain environment without control-plane state."""

    environment = project_environment(project_path)
    stripped = tuple(
        sorted(
            name
            for name in environment
            if name.startswith(CONTROL_PLANE_ENV_PREFIXES) or name in CONTROL_PLANE_ENV_NAMES
        )
    )
    for name in stripped:
        del environment[name]
    return environment, stripped


__all__ = [
    "CONTROL_PLANE_ENV_NAMES",
    "CONTROL_PLANE_ENV_PREFIXES",
    "installed_node_environment",
    "project_environment",
    "verification_gate_environment",
]
