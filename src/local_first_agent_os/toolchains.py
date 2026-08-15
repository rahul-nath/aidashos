# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-project toolchain resolution for external agent worktrees."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

_EXACT_NODE_VERSION = re.compile(r"(?:v)?(\d+\.\d+\.\d+)")


def project_environment(
    project_path: Path,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment honoring an exact ``.nvmrc`` when present."""

    env = {**os.environ, **(overrides or {})}
    version_file = project_path / ".nvmrc"
    if not version_file.is_file():
        return env
    raw = version_file.read_text(encoding="utf-8").strip()
    match = _EXACT_NODE_VERSION.fullmatch(raw)
    if match is None:
        raise RuntimeError(f"{version_file} must contain an exact Node version.")
    version = match.group(1)
    nvm_dir = Path(env.get("NVM_DIR") or (Path.home() / ".nvm")).expanduser()
    node_bin = nvm_dir / "versions" / "node" / f"v{version}" / "bin"
    node = node_bin / "node"
    if not node.is_file():
        raise RuntimeError(
            f"Node {version} pinned by {version_file} is not installed. "
            f"Run `source ~/.nvm/nvm.sh && nvm install {version}`."
        )
    path_parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    env["PATH"] = os.pathsep.join((str(node_bin), *path_parts))
    env["LOCAL_AGENT_NODE_VERSION"] = version
    return env


__all__ = ["project_environment"]
