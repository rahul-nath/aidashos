# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Build a reproducible source candidate from an exact curated public commit."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import subprocess
import tarfile
import tomllib
from pathlib import Path

from pydantic import TypeAdapter

from .release_contract import GitCommit, Version, file_sha256


def _git(source: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(source), *arguments], capture_output=True, check=True
    ).stdout


def build_source_candidate(source: Path, output: Path) -> Path:
    source = source.resolve()
    if _git(source, "status", "--porcelain").strip():
        raise ValueError("release source must be clean, including untracked files")
    commit = TypeAdapter(GitCommit).validate_python(
        _git(source, "rev-parse", "HEAD").decode().strip()
    )
    paths = set(_git(source, "ls-tree", "-r", "--name-only", "HEAD").decode().splitlines())
    if not {"public_import.toml", "LICENSE", "pyproject.toml", "uv.lock"} <= paths:
        raise ValueError(
            "release requires the curated public mirror, its LICENSE and dependency lock"
        )
    metadata = tomllib.loads(_git(source, "show", "HEAD:pyproject.toml").decode())
    version = TypeAdapter(Version).validate_python(metadata["project"]["version"])
    archive = _git(source, "archive", "--format=tar", f"--prefix=aidashos-{version}/", commit)
    # A public tree may contain symlinks, but a source release must not write
    # outside its extraction root or carry tracked operator state.
    with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
        for member in contents:
            relative = Path(member.name).relative_to(f"aidashos-{version}")
            if member.issym() or member.islnk():
                raise ValueError(f"release symlink requires explicit packaging support: {relative}")
            if any(
                part in {".git", ".local_agent", ".env", "operator.token"}
                for part in relative.parts
            ):
                raise ValueError(f"operator state is not distributable: {relative}")
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f"aidashos-{version}-{commit[:12]}.tar.gz"
    manifest_path = artifact.with_suffix(".manifest.json")
    if artifact.exists() or manifest_path.exists():
        raise FileExistsError("candidate already exists; choose an empty output directory")
    with (
        artifact.open("xb") as target,
        gzip.GzipFile(fileobj=target, mode="wb", mtime=0, filename="") as compressed,
    ):
        compressed.write(archive)
    manifest = {
        "schema_version": "source_candidate.v1",
        "version": version,
        "tag": f"v{version}",
        "public_commit": commit,
        "artifact": artifact.name,
        "artifact_sha256": file_sha256(artifact),
        "qualification": "not_assessed",
    }
    with manifest_path.open("x") as target:
        target.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="clean curated public checkout")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(build_source_candidate(arguments.source, arguments.output))


if __name__ == "__main__":
    main()
