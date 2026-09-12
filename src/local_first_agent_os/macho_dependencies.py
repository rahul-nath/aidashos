# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Resolve native loader edges into exact files, without granting search directories."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_LOAD_COMMANDS = frozenset(
    {
        "LC_LOAD_DYLIB",
        "LC_LOAD_WEAK_DYLIB",
        "LC_REEXPORT_DYLIB",
        "LC_LOAD_UPWARD_DYLIB",
        "LC_LAZY_LOAD_DYLIB",
    }
)


@dataclass(frozen=True)
class _Image:
    dependencies: tuple[str, ...]
    runpaths: tuple[str, ...]


def _image(path: Path) -> _Image:
    result = subprocess.run(
        ("/usr/bin/otool", "-arch", platform.machine(), "-l", str(path)),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"verification image is not readable native code: {path}")
    dependencies, runpaths = [], []
    command = ""
    command_count = 0
    for line in result.stdout.splitlines():
        value = line.strip()
        if value.startswith("cmd "):
            command_count += 1
            if command:
                raise ValueError(f"verification image has a malformed load command: {path}")
            kind = value.removeprefix("cmd ")
            if kind == "LC_DYLD_ENVIRONMENT":
                raise ValueError(f"verification image overrides its loader environment: {path}")
            command = kind if kind in _LOAD_COMMANDS or kind == "LC_RPATH" else ""
        elif command and value.startswith("path " if command == "LC_RPATH" else "name "):
            name, separator, _offset = value.split(" ", 1)[1].rpartition(" (offset ")
            if not separator or not name:
                raise ValueError(f"verification image has a malformed loader path: {path}")
            (runpaths if command == "LC_RPATH" else dependencies).append(name)
            command = ""
    if command or not command_count:
        raise ValueError(f"verification image has incomplete native load commands: {path}")
    return _Image(tuple(dependencies), tuple(runpaths))


def _anchored(reference: str, loader: Path, executable: Path) -> Path:
    for token, anchor in (("@loader_path", loader.parent), ("@executable_path", executable.parent)):
        if reference == token or reference.startswith(token + "/"):
            return Path(os.path.normpath(anchor / reference.removeprefix(token).lstrip("/")))
    if reference.startswith("/"):
        return Path(os.path.normpath(reference))
    raise ValueError(f"unsupported verification loader reference {reference!r} in {loader}")


def _system(path: Path) -> bool:
    return path.is_relative_to("/System") or path.is_relative_to("/usr/lib")


def linked_runtime_references(executable: Path) -> dict[Path, Path]:
    """Follow host-architecture load commands with the loader's inherited runpath stack.

    LC_ID_DYLIB names the image itself, so it is never a dependency edge.
    System shared-cache images need no additional grant and may have no on-disk file.
    Unknown or missing dependencies fail closed with the requesting image and reference.
    """
    executable = executable.resolve(strict=True)
    images: dict[Path, _Image] = {}
    visited: set[tuple[Path, tuple[Path, ...]]] = set()
    discovered: dict[Path, Path] = {executable: executable}
    pending: list[tuple[Path, tuple[Path, ...], frozenset[Path]]] = [(executable, (), frozenset())]
    while pending:
        current, inherited, ancestors = pending.pop()
        if current in ancestors or (current, inherited) in visited:
            continue
        visited.add((current, inherited))
        if current not in images:
            images[current] = _image(current)
        image = images[current]
        runpaths = tuple(
            dict.fromkeys(
                (
                    *(_anchored(value, current, executable) for value in image.runpaths),
                    *inherited,
                )
            )
        )
        for reference in image.dependencies:
            if reference.startswith("@rpath/"):
                if reference[7:].startswith("/") or not reference[7:]:
                    raise ValueError(
                        f"malformed verification runpath reference {reference!r} in {current}"
                    )
                candidates = tuple(
                    Path(os.path.normpath(root / reference[7:])) for root in runpaths
                )
            else:
                candidates = (_anchored(reference, current, executable),)
            candidate = None
            for path in candidates:
                present = path.is_file()
                if reference.startswith("@rpath/") and _system(path) and not present:
                    raise ValueError(
                        f"unresolved system-cache runpath candidate {path} in {current}; "
                        "cannot safely select a later search path"
                    )
                if present or _system(path):
                    candidate = path
                    break
            if candidate is None:
                raise ValueError(
                    f"unresolved verification library {reference!r} in {current}; "
                    f"searched: {', '.join(map(str, candidates)) or '(no runpaths)'}"
                )
            if _system(candidate):
                continue
            resolved = candidate.resolve(strict=True)
            if not resolved.is_file():
                raise ValueError(f"verification dependency is not a file: {candidate}")
            discovered[candidate] = resolved
            pending.append((resolved, runpaths, ancestors | {current}))
    return discovered


def linked_runtime_files(executable: Path) -> tuple[Path, ...]:
    return tuple(sorted(set(linked_runtime_references(executable).values())))


@dataclass(frozen=True)
class PinnedRuntimeFile:
    path: Path
    references: tuple[Path, ...]
    sha256: str
    device: int
    inode: int
    size: int

    @classmethod
    def capture(
        cls, source: Path, *, published_path: Path | None = None, references: tuple[Path, ...] = ()
    ) -> PinnedRuntimeFile:
        path = (published_path or source).resolve()
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("runtime dependency must be a regular file")
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return cls(path, references or (path,), digest, info.st_dev, info.st_ino, info.st_size)

    def verify(self) -> None:
        if any(reference.resolve(strict=True) != self.path for reference in self.references):
            raise ValueError(f"runtime dependency alias changed: {self.path}")
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino, info.st_size) != (
                self.device,
                self.inode,
                self.size,
            ):
                raise ValueError(f"runtime dependency identity changed: {self.path}")
            if hashlib.file_digest(stream, "sha256").hexdigest() != self.sha256:
                raise ValueError(f"runtime dependency bytes changed: {self.path}")

    def payload(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "references": list(map(str, self.references)),
            "sha256": self.sha256,
            "device": self.device,
            "inode": self.inode,
            "size": self.size,
        }


@dataclass(frozen=True)
class RuntimeDependencyClosure:
    """Host-selected loader files; no child request can supply this declaration."""

    executable: PinnedRuntimeFile
    dependencies: tuple[PinnedRuntimeFile, ...]

    def selected(self, command: Sequence[str], environment: Mapping[str, str]) -> bool:
        for name in (str(command[0]) if command else "", self.executable.path.name):
            found = shutil.which(name, path=environment.get("PATH", ""))
            if found is not None and Path(found).resolve() == self.executable.path:
                return True
        return False

    def verified_reads(self) -> tuple[Path, ...]:
        for entry in (self.executable, *self.dependencies):
            entry.verify()
        return tuple(entry.path for entry in self.dependencies)


def runtime_dependency_reads(
    declarations: tuple[RuntimeDependencyClosure, ...],
    command: Sequence[str],
    environment: Mapping[str, str],
) -> tuple[Path, ...]:
    return tuple(
        path
        for closure in declarations
        if closure.selected(command, environment)
        for path in closure.verified_reads()
    )
