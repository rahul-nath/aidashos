# SPDX-License-Identifier: AGPL-3.0-or-later
"""Copy an already installed Node distribution into CI's private NVM layout."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def node_version(executable: Path) -> str:
    return subprocess.run(
        (str(executable), "--version"), check=True, capture_output=True, text=True
    ).stdout.strip()


def prepare(root: Path, source_node: Path, nvm_dir: Path) -> Path:
    version = (root / ".nvmrc").read_text().strip().removeprefix("v")
    if re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise ValueError(".nvmrc must pin one exact Node version")
    source_node = source_node.resolve(strict=True)
    if source_node.name != "node" or source_node.parent.name != "bin":
        raise ValueError("Node must belong to an installed distribution's bin directory")
    source = source_node.parent.parent
    if source in {Path("/"), Path("/usr"), Path("/usr/local"), Path("/opt/homebrew")}:
        raise ValueError("refusing to copy a shared system prefix as a Node distribution")
    if node_version(source_node) != f"v{version}":
        raise ValueError("installed Node does not match .nvmrc")
    destination = nvm_dir.resolve() / "versions" / "node" / f"v{version}"
    if destination.exists() or destination.is_symlink():
        raise ValueError("CI Node destination must be fresh")
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("CI Node destination overlaps the installed distribution")
    for entry in source.rglob("*"):
        if entry.is_symlink() and (
            entry.readlink().is_absolute() or not entry.resolve().is_relative_to(source)
        ):
            raise ValueError("Node distribution contains a link outside its copied layout")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".node-copy-") as scratch:
        staged = Path(scratch) / "distribution"
        shutil.copytree(source, staged, symlinks=True)
        if node_version(staged / "bin" / "node") != f"v{version}":
            raise ValueError("copied Node cannot execute its pinned version")
        staged.rename(destination)
    return destination / "bin" / "node"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--node", type=Path, default=shutil.which("node"))
    parser.add_argument("--nvm-dir", type=Path, default=os.environ.get("NVM_DIR"))
    args = parser.parse_args(argv)
    if args.node is None or args.nvm_dir is None:
        parser.error("installed node on PATH and explicit NVM_DIR (or CLI equivalents) required")
    try:
        executable = prepare(args.root.resolve(strict=True), args.node, args.nvm_dir)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"CI Node setup refused: {error}", file=sys.stderr)
        return 1
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
