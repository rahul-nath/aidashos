# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Relocate already-installed verification runtimes using only operator permissions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .macho_dependencies import (
    PinnedRuntimeFile,
    RuntimeDependencyClosure,
    linked_runtime_files,
    linked_runtime_references,
)

if TYPE_CHECKING:
    from .host_verification import _InstalledToolchain
    from .uid_verifier_client import UidVerifierClient


@dataclass(frozen=True)
class PublicVerificationCa:
    """The host resource owner declares only its public PostgreSQL trust bundle."""

    path: Path

    def __post_init__(self) -> None:
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise ValueError("public verification CA requires an absolute source path")


@dataclass(frozen=True)
class StagedVerificationCa:
    source: PublicVerificationCa
    copied: PinnedRuntimeFile

    def verified_path(self, source: PublicVerificationCa) -> Path:
        if source != self.source:
            raise ValueError("staged verification CA belongs to a different resource")
        self.copied.verify()
        return self.copied.path


@dataclass(frozen=True)
class _DirectoryIdentity:
    device: int
    inode: int
    owner: int

    @classmethod
    def observed(cls, info: os.stat_result) -> _DirectoryIdentity:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("staged cleanup identity must name a directory")
        return cls(info.st_dev, info.st_ino, info.st_uid)


@dataclass(frozen=True)
class StagedToolchain:
    toolchain: _InstalledToolchain
    manifest: Path
    manifest_digest: str
    anchor: Path
    anchor_identity: _DirectoryIdentity
    installed_identity: _DirectoryIdentity
    runtime_dependencies: tuple[RuntimeDependencyClosure, ...] = ()
    public_ca: StagedVerificationCa | None = None

    def cleanup_after(self, owner: UidVerifierClient) -> None:
        """Remove only the original copied tree, after every owned UID is absent."""
        owner.require_closed_staging(self.anchor)
        if os.geteuid() == 0 or os.geteuid() != self.anchor_identity.owner:
            raise PermissionError("staged cleanup requires its original unprivileged operator")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("staged cleanup requires descriptor-based symlink-safe rmtree")
        descriptor = os.open(self.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if _DirectoryIdentity.observed(os.fstat(descriptor)) != self.anchor_identity:
                raise ValueError("staging anchor identity changed before cleanup")
            installed = os.stat("installed", dir_fd=descriptor, follow_symlinks=False)
            if _DirectoryIdentity.observed(installed) != self.installed_identity:
                raise ValueError("installed copy identity changed before cleanup")
            manifest_fd = os.open(
                "provenance.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
            )
            with os.fdopen(manifest_fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("staged provenance is no longer a regular file")
                if hashlib.file_digest(stream, "sha256").hexdigest() != self.manifest_digest:
                    raise ValueError("staged provenance changed before cleanup")
            shutil.rmtree("installed", dir_fd=descriptor)
        finally:
            os.close(descriptor)

    def payload(self) -> dict[str, object]:
        return {
            "environment": str(self.toolchain.environment),
            "python": str(self.toolchain.environment / "bin" / "python"),
            "executables": [str(path) for path in self.toolchain.executables],
            "readable": [str(path) for path in self.toolchain.readable],
            "manifest": str(self.manifest),
            "manifest_digest": self.manifest_digest,
        }


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _copy_regular_file(source: Path, target: Path) -> str:
    """Record the source bytes actually copied, without a second full source read.

    A fresh regular file inherits the gate anchor's ACL instead of source ACLs or
    system flags, and never shares a mutable inode with an installed runtime.
    The final manifest independently hashes staged bytes after path rewrites.
    """
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as reader:
        info = os.fstat(reader.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("installed runtime copy source must be a regular file")
        with target.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
                digest.update(chunk)
    target.chmod(stat.S_IMODE(info.st_mode) & 0o777)
    return digest.hexdigest()


def stage_installed_toolchain(
    project_root: Path,
    destination: Path,
    *,
    source_root: Path | None = None,
    public_ca: PublicVerificationCa | None = None,
) -> StagedToolchain:
    """Publish one fresh relocation without changing an installed runtime or its parents.

    The caller must already be the unprivileged operator.
    Destination is a fresh operator-owned helper anchor with inherited gate access.
    Complete runtime bytes are copied locally; only recorded launch-path metadata changes.
    """
    from .host_verification import _installed_toolchain, _InstalledToolchain

    if os.geteuid() == 0:
        raise PermissionError("installed toolchain staging must run as the unprivileged operator")
    destination = destination.absolute()
    info = destination.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or any(destination.iterdir())
    ):
        raise ValueError("toolchain staging requires a fresh operator-owned directory")
    destination = destination.resolve(strict=True)
    original = _installed_toolchain(project_root.resolve(), source_root=source_root)
    base = Path(sys.base_prefix).resolve(strict=True)
    final = destination / "installed"
    pending = Path(tempfile.mkdtemp(prefix="pending-", dir=destination))
    git_prefix = original.executables[1].parent.parent
    mappings = (
        (original.environment, final / "environment"),
        (base, final / "python-base"),
        (git_prefix, final / "git"),
    )
    origins: dict[str, dict[str, str]] = {}
    try:
        ca_relative = Path("public-resources") / "postgres-ca.pem"
        if public_ca is not None:
            # This public trust bundle is data, never a credential or executable.
            # Copy into the existing inherited gate ACL without changing host ancestors.
            descriptor = os.open(public_ca.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as reader:
                source_info = os.fstat(reader.fileno())
                if (
                    not stat.S_ISREG(source_info.st_mode)
                    or not 0 < source_info.st_size <= 4 * 1024 * 1024
                ):
                    raise ValueError("public verification CA must be a bounded regular file")
                contents = reader.read(4 * 1024 * 1024 + 1)
                if len(contents) != source_info.st_size:
                    raise ValueError("public verification CA changed size during copying")
            copied_ca = pending / ca_relative
            copied_ca.parent.mkdir(mode=0o700)
            with copied_ca.open("xb") as writer:
                writer.write(contents)
            copied_ca.chmod(0o400)
            origins[str(ca_relative)] = {
                "source": str(public_ca.path),
                "source_sha256": hashlib.sha256(contents).hexdigest(),
                "public_resource_kind": "postgres_tls_ca",
            }
        for source, target in mappings:
            relative_root = target.relative_to(final)

            def copy_file(raw_source: str, raw_target: str) -> str:
                source_file, target_file = Path(raw_source), Path(raw_target)
                source_digest = _copy_regular_file(source_file, target_file)
                key = str(target_file.relative_to(pending))
                origins[key] = {"source": str(source_file), "source_sha256": source_digest}
                return str(target_file)

            shutil.copytree(source, pending / relative_root, symlinks=True, copy_function=copy_file)
        staged_executables: list[Path] = []
        executable_mappings: dict[Path, Path] = {}
        for name, source in zip(("uv", "git", "node"), original.executables, strict=True):
            if name == "git":
                target = final / "git" / source.relative_to(git_prefix)
                staged_executables.append(target)
                executable_mappings[source] = target
                continue
            # Mirror exact native files at their original relative locations.
            # Moving only the executable breaks @loader_path/../lib and @rpath.
            for dependency in linked_runtime_files(source):
                relative = Path("native") / dependency.relative_to(dependency.anchor)
                copied = pending / relative
                if not copied.exists():
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    origins[str(relative)] = {
                        "source": str(dependency),
                        "source_sha256": _copy_regular_file(dependency, copied),
                    }
            target = final / "native" / source.relative_to(source.anchor)
            staged_executables.append(target)
            executable_mappings[source] = target

        # Resolve the relocated binaries before publishing them. A loader edge may
        # still be absolute, but it must name one of the original exact-file grants.
        allowed_host_files = {path.resolve() for path in original.readable if path.is_file()}
        external_loader_references: dict[Path, dict[Path, Path]] = {}
        for executable in staged_executables:
            copied_executable = pending / executable.relative_to(final)
            external_files: set[Path] = set()
            for dependency in linked_runtime_files(copied_executable):
                if dependency.is_relative_to(pending):
                    if str(dependency.relative_to(pending)) not in origins:
                        raise ValueError(
                            f"relocated loader reached an unrecorded file: {dependency}"
                        )
                elif dependency not in allowed_host_files:
                    raise ValueError(
                        f"relocated loader escaped its exact-file grants: {dependency}"
                    )
                else:
                    external_files.add(dependency)
            if external_files:
                references = {
                    alias: resolved
                    for alias, resolved in linked_runtime_references(copied_executable).items()
                    if not resolved.is_relative_to(pending)
                }
                if set(references.values()) != external_files:
                    raise ValueError("relocated runtime dependency closure changed during staging")
                external_loader_references[executable] = references

        def relocated(path: Path) -> Path | None:
            lexical = path.absolute()
            for source, target in mappings:
                if lexical.is_relative_to(source):
                    return target / lexical.relative_to(source)
            path = path.resolve(strict=True)
            if path in executable_mappings:
                return executable_mappings[path]
            for source, target in mappings:
                if path.is_relative_to(source):
                    return target / path.relative_to(source)
            return None

        for source, target in mappings:
            copied_root = pending / target.relative_to(final)
            for copied in copied_root.rglob("*"):
                if not copied.is_symlink():
                    continue
                source_link = source / copied.relative_to(copied_root)
                original_target = source_link.resolve(strict=True)
                target_path = relocated(original_target)
                if target_path is None:
                    raise ValueError(f"installed runtime symlink escapes relocation: {source_link}")
                final_link = final / copied.relative_to(pending)
                copied.unlink()
                copied.symlink_to(os.path.relpath(target_path, final_link.parent))
                link_info = copied.lstat()
                if (link_info.st_uid, link_info.st_gid) != (info.st_uid, info.st_gid):
                    raise ValueError("staged symlink did not inherit the operator and gate group")
                # Darwin authorizes explicit readlink separately from following
                # a link during exec. Symlinks do not retain the inherited ACL,
                # so a private umask must not remove the gate group's inspection.
                link_mode = stat.S_IMODE(link_info.st_mode) | stat.S_IRGRP
                if link_mode != stat.S_IMODE(link_info.st_mode):
                    copied.chmod(link_mode, follow_symlinks=False)
                origins[str(copied.relative_to(pending))] = {
                    "source": str(source_link),
                    "source_link": os.readlink(source_link),
                    "staged_link": os.readlink(copied),
                    "staged_link_mode": oct(stat.S_IMODE(copied.lstat().st_mode)),
                }

        environment = final / "environment"
        copied_environment = pending / "environment"
        configuration = copied_environment / "pyvenv.cfg"
        if not configuration.is_file():
            raise ValueError("installed verification environment lacks pyvenv.cfg")
        lines = []
        for line in configuration.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() in {"home", "executable"}:
                mapped = relocated(Path(value.strip()))
                if mapped is None:
                    raise ValueError("virtual environment startup path is outside staged runtimes")
                line = key + "= " + str(mapped)
            lines.append(line)
        configuration.write_text("\n".join(lines) + "\n")

        for copied in (copied_environment / "bin").iterdir():
            if copied.is_symlink() or not copied.is_file():
                continue
            with copied.open("rb") as stream:
                first = stream.readline(4096)
            if not first.startswith(b"#!"):
                continue
            interpreter = first[2:].strip().decode("utf-8", errors="strict")
            if not interpreter.startswith("/") or not Path(interpreter).exists():
                continue
            mapped = relocated(Path(interpreter))
            if mapped is not None:
                content = copied.read_bytes()
                copied.write_bytes(b"#!" + str(mapped).encode() + b"\n" + content[len(first) :])

        project_sources = {
            (project_root / "src").resolve(),
            (original.environment.parent / "src").resolve(),
        }
        for copied in copied_environment.rglob("*.pth"):
            lines = []
            for line in copied.read_text().splitlines():
                if line.startswith("/"):
                    path = Path(line).resolve(strict=True)
                    if path in project_sources:
                        if source_root is None or not source_root.is_dir():
                            raise ValueError(
                                "editable project relocation requires its frozen source root"
                            )
                        mapped = source_root.resolve() / "src"
                    else:
                        mapped = relocated(path)
                        if mapped is None:
                            raise ValueError(
                                "editable runtime path escapes the staged source and toolchain"
                            )
                    line = str(mapped)
                lines.append(line)
            copied.write_text("\n".join(lines) + "\n")

        staged_ca = (
            StagedVerificationCa(
                public_ca,
                PinnedRuntimeFile.capture(
                    pending / ca_relative, published_path=final / ca_relative
                ),
            )
            if public_ca is not None
            else None
        )
        if (
            staged_ca is not None
            and staged_ca.copied.sha256 != origins[str(ca_relative)]["source_sha256"]
        ):
            raise ValueError("staged public verification CA differs from its captured source")
        runtime_dependencies = tuple(
            RuntimeDependencyClosure(
                PinnedRuntimeFile.capture(
                    pending / executable.relative_to(final), published_path=executable
                ),
                tuple(
                    PinnedRuntimeFile.capture(
                        dependency,
                        references=tuple(
                            sorted(
                                alias
                                for alias, target in references.items()
                                if target == dependency
                            )
                        ),
                    )
                    for dependency in sorted(set(references.values()))
                ),
            )
            for executable, references in external_loader_references.items()
        )
        dependencies_by_image = {
            str(closure.executable.path.relative_to(final)): closure
            for closure in runtime_dependencies
        }
        manifest: list[dict[str, object]] = []
        for copied in sorted(pending.rglob("*")):
            if copied.is_symlink():
                manifest.append(
                    {
                        "path": str(copied.relative_to(pending)),
                        **origins[str(copied.relative_to(pending))],
                    }
                )
            elif copied.is_file():
                key = str(copied.relative_to(pending))
                row: dict[str, object] = {
                    "path": key,
                    **origins[key],
                    "staged_sha256": _hash(copied),
                }
                if key in dependencies_by_image:
                    closure = dependencies_by_image[key]
                    row["native_runtime_closure"] = {
                        "executable": closure.executable.payload(),
                        "dependencies": [entry.payload() for entry in closure.dependencies],
                    }
                manifest.append(row)
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        pending.rename(final)
        manifest_path = destination / "provenance.json"
        with manifest_path.open("xb") as manifest_stream:
            manifest_stream.write(encoded)
            manifest_stream.flush()
            os.fsync(manifest_stream.fileno())
        # Retain exact shared-library/Git-helper grants, excluding replaced private runtime roots.
        retained = tuple(
            path
            for path in original.readable
            if not any(path.resolve().is_relative_to(source) for source, _ in mappings)
            and path.resolve() not in executable_mappings
        )
        staged = _InstalledToolchain(
            environment,
            tuple(staged_executables),
            (final / "python-base", environment, final / "native", final / "git", *retained),
        )
        return StagedToolchain(
            staged,
            manifest_path,
            hashlib.sha256(encoded).hexdigest(),
            destination,
            _DirectoryIdentity.observed(info),
            _DirectoryIdentity.observed(final.lstat()),
            runtime_dependencies,
            staged_ca,
        )
    except BaseException:
        if pending.exists():
            shutil.rmtree(pending)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--source", type=Path)
    arguments = parser.parse_args()
    staged = stage_installed_toolchain(
        arguments.project, arguments.destination, source_root=arguments.source
    )
    print(json.dumps(staged.payload(), sort_keys=True))


if __name__ == "__main__":
    main()
