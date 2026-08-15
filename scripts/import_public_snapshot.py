#!/usr/bin/env python3
"""Copy an explicit allowlist from a private checkout into this public snapshot."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1

_ROOT_KEYS = frozenset({"schema_version", "source_root", "destination_root", "allow"})
_ENTRY_KEYS = frozenset({"path", "kind", "overwrite"})
_BLOCKED_COMPONENTS = frozenset({".git", ".hg", ".svn"})
_IGNORED_DIRECTORY_NAMES = frozenset(
    {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv"}
)
_IGNORED_FILE_NAMES = frozenset({".DS_Store"})
_SAFE_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template")
_COPY_CHUNK_BYTES = 1024 * 1024

# Paths whose content describes the operator - their machine, their projects,
# their process - rather than the system. Withholding is reported rather than
# silent: a file that quietly failed to arrive is the failure mode this guard
# exists to prevent.
#
# linked_projects.toml is the operator's own registry of private repositories.
# The runtime fails closed without a file at that path, so the snapshot keeps
# this repository's own example copy and never receives the private one.
#
# The release checklist is withheld for a sharper reason: its pre-snapshot scan
# command enumerates, as grep patterns, the exact client names the private
# repository polices for. Publishing the procedure would publish the list of
# things the procedure exists to keep private.
_OPERATOR_STATE_PATHS = frozenset(
    {
        "configs/linked_projects.toml",
        "docs/public_release_checklist.md",
    }
)

# Paths whose content is not this project's to give away. Withheld the same way
# operator state is - reported, never silent - and named separately so the
# report does not describe somebody else's material as the operator's.
#
# design_principles.md is distilled from the Mirdin Advanced Software Design
# course material and Torbjorn Gannholm's talk notes, whose authors asked that
# they not be shared. The bounded contract derived from it,
# src/local_first_agent_os/engineering_doctrine.py, does ship: that one is this
# project's own writing, it reads as general engineering advice a reader can
# adapt or replace, and it is the form the system actually injects into a task.
# Withholding the distillation and publishing the contract is the line between
# repeating someone's course and shipping what this project made of it.
_THIRD_PARTY_PATHS = frozenset({"docs/design_principles.md"})


class SnapshotError(RuntimeError):
    """Base class for a rejected import."""


class ManifestError(SnapshotError):
    """The manifest cannot represent a valid import."""


class SafetyError(SnapshotError):
    """The import crosses a public-snapshot safety boundary."""


class ConflictError(SnapshotError):
    """A destination file differs and replacement was not authorized."""


class EntryKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


class ActionKind(StrEnum):
    CREATE = "create"
    REPLACE = "replace"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class AllowEntry:
    path: PurePosixPath
    kind: EntryKind
    overwrite: bool


@dataclass(frozen=True, slots=True)
class ImportManifest:
    source_root: Path
    destination_root: Path
    entries: tuple[AllowEntry, ...]


@dataclass(frozen=True, slots=True)
class FileAction:
    source: Path
    destination: Path
    relative_path: PurePosixPath
    kind: ActionKind
    source_sha256: str
    destination_sha256: str | None
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ImportPlan:
    manifest: ImportManifest
    actions: tuple[FileAction, ...]
    ignored_paths: tuple[PurePosixPath, ...]
    withheld_paths: tuple[PurePosixPath, ...]

    def count(self, kind: ActionKind) -> int:
        return sum(action.kind is kind for action in self.actions)

    @property
    def bytes_to_write(self) -> int:
        return sum(
            action.size_bytes
            for action in self.actions
            if action.kind in {ActionKind.CREATE, ActionKind.REPLACE}
        )


def _require_mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{context} must be a TOML table.")
    return value


def _reject_unknown_keys(
    payload: Mapping[str, Any],
    allowed_keys: frozenset[str],
    context: str,
) -> None:
    unknown = sorted(set(payload) - allowed_keys)
    if unknown:
        raise ManifestError(f"{context} has unknown keys: {', '.join(unknown)}")


def _require_string(payload: Mapping[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{context}.{key} must be a non-empty string.")
    return value.strip()


def _resolve_root(raw: str, manifest_dir: Path, *, must_exist: bool, field: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if "$" in expanded:
        raise ManifestError(f"{field} contains an unresolved environment variable: {raw}")
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = manifest_dir / candidate
    resolved = candidate.resolve(strict=must_exist)
    if must_exist and not resolved.is_dir():
        raise ManifestError(f"{field} is not a directory: {resolved}")
    if not must_exist and resolved.exists() and not resolved.is_dir():
        raise ManifestError(f"{field} is not a directory: {resolved}")
    return resolved


def _parse_relative_path(raw: str, context: str) -> PurePosixPath:
    if "\\" in raw:
        raise ManifestError(f"{context}.path must use forward slashes.")
    segments = raw.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ManifestError(
            f"{context}.path must be normalized and cannot contain '.', '..', or '//'."
        )
    path = PurePosixPath(raw)
    if path.is_absolute():
        raise ManifestError(f"{context}.path must be relative: {raw}")
    blocked = sorted(set(path.parts) & _BLOCKED_COMPONENTS)
    if blocked:
        raise ManifestError(
            f"{context}.path contains forbidden version-control metadata: {', '.join(blocked)}"
        )
    if _is_environment_file(path.name):
        raise ManifestError(f"{context}.path cannot directly allowlist an environment file.")
    return path


def _paths_overlap(first: PurePosixPath, second: PurePosixPath) -> bool:
    return first == second or first in second.parents or second in first.parents


def _validate_entries_do_not_overlap(entries: Sequence[AllowEntry]) -> None:
    for index, first in enumerate(entries):
        for second in entries[index + 1 :]:
            if _paths_overlap(first.path, second.path):
                raise ManifestError(
                    f"Allowlist entries overlap: {first.path.as_posix()} and "
                    f"{second.path.as_posix()}"
                )


def load_manifest(manifest_path: Path) -> ImportManifest:
    """Parse and validate a public-import manifest without writing anything."""

    resolved_manifest = manifest_path.expanduser().resolve(strict=True)
    try:
        with resolved_manifest.open("rb") as handle:
            raw_payload = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ManifestError(f"Invalid TOML in {resolved_manifest}: {error}") from error

    payload = _require_mapping(raw_payload, "manifest")
    _reject_unknown_keys(payload, _ROOT_KEYS, "manifest")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(f"manifest.schema_version must equal {SCHEMA_VERSION}.")

    source_root = _resolve_root(
        _require_string(payload, "source_root", "manifest"),
        resolved_manifest.parent,
        must_exist=True,
        field="manifest.source_root",
    )
    destination_root = _resolve_root(
        _require_string(payload, "destination_root", "manifest"),
        resolved_manifest.parent,
        must_exist=False,
        field="manifest.destination_root",
    )
    if (
        source_root == destination_root
        or source_root.is_relative_to(destination_root)
        or destination_root.is_relative_to(source_root)
    ):
        raise ManifestError("Source and destination roots must not overlap.")

    raw_entries = payload.get("allow")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ManifestError("manifest.allow must contain at least one [[allow]] table.")

    entries: list[AllowEntry] = []
    for index, raw_entry in enumerate(raw_entries):
        context = f"manifest.allow[{index}]"
        entry = _require_mapping(raw_entry, context)
        _reject_unknown_keys(entry, _ENTRY_KEYS, context)
        path = _parse_relative_path(_require_string(entry, "path", context), context)
        raw_kind = _require_string(entry, "kind", context)
        try:
            kind = EntryKind(raw_kind)
        except ValueError as error:
            supported = ", ".join(item.value for item in EntryKind)
            raise ManifestError(f"{context}.kind must be one of: {supported}") from error
        overwrite = entry.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise ManifestError(f"{context}.overwrite must be true or false.")
        entries.append(AllowEntry(path=path, kind=kind, overwrite=overwrite))

    _validate_entries_do_not_overlap(entries)
    return ImportManifest(
        source_root=source_root,
        destination_root=destination_root,
        entries=tuple(entries),
    )


def _is_environment_file(name: str) -> bool:
    if name == ".env":
        return True
    return name.startswith(".env.") and not name.endswith(_SAFE_ENV_TEMPLATE_SUFFIXES)


def _validate_public_component(name: str, relative_path: PurePosixPath) -> None:
    if name in _BLOCKED_COMPONENTS:
        raise SafetyError(f"Version-control metadata is forbidden: {relative_path.as_posix()}")
    if _is_environment_file(name):
        raise SafetyError(f"Environment files are forbidden: {relative_path.as_posix()}")


def _is_ignored_file(path: Path) -> bool:
    return path.name in _IGNORED_FILE_NAMES or path.suffix in {".pyc", ".pyo"}


def _is_operator_state(relative_path: PurePosixPath) -> bool:
    return relative_path.as_posix() in _OPERATOR_STATE_PATHS


def _is_withheld(relative_path: PurePosixPath) -> bool:
    return _is_operator_state(relative_path) or _is_third_party(relative_path)


def _is_third_party(relative_path: PurePosixPath) -> bool:
    return relative_path.as_posix() in _THIRD_PARTY_PATHS


def withheld_reason(relative_path: PurePosixPath) -> str:
    """Why a path did not travel, in the words the report prints.

    A reason rather than a flag, because the two answer different questions for
    whoever reads the log. Operator state is absent because it describes this
    machine; third-party material is absent because it is not this project's to
    give away. Both may leave a public counterpart behind at the same path -
    `configs/linked_projects.toml` keeps an example registry, and
    `docs/design_principles.md` keeps an empty file inviting a reader to write
    their own - so the reason says why it was withheld and never implies
    anything about whether something is there instead.
    """

    return "third-party material" if _is_third_party(relative_path) else "operator state"


def _source_node_kind(path: Path, relative_path: PurePosixPath) -> EntryKind:
    if path.is_symlink():
        raise SafetyError(f"Symlinks are forbidden: {relative_path.as_posix()}")
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except FileNotFoundError as error:
        raise ManifestError(
            f"Allowlisted source does not exist: {relative_path.as_posix()}"
        ) from error
    if stat.S_ISREG(mode):
        return EntryKind.FILE
    if stat.S_ISDIR(mode):
        return EntryKind.DIRECTORY
    raise SafetyError(f"Special filesystem nodes are forbidden: {relative_path.as_posix()}")


def _source_path(source_root: Path, relative_path: PurePosixPath) -> Path:
    current = source_root
    for component in relative_path.parts:
        current /= component
        if current.is_symlink():
            raise SafetyError(f"Symlinks are forbidden: {relative_path.as_posix()}")
    try:
        resolved = current.resolve(strict=True)
    except FileNotFoundError as error:
        raise ManifestError(
            f"Allowlisted source does not exist: {relative_path.as_posix()}"
        ) from error
    if not resolved.is_relative_to(source_root):
        raise SafetyError(f"Source escapes its root: {relative_path.as_posix()}")
    return current


@cache
def _collect_gitignored(source_root: Path) -> frozenset[str]:
    """Relative paths (dirs end in '/') that source_root's own .gitignore rules exclude.

    Shells out to git rather than reimplementing gitignore matching, so nested
    .gitignore files (e.g. web/.gitignore) resolve exactly as git itself would.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(source_root),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
                "-z",
            ],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return frozenset()
    return frozenset(entry for entry in result.stdout.decode().split("\0") if entry)


def _is_git_ignored(relative_path: PurePosixPath, gitignored: frozenset[str]) -> bool:
    posix = relative_path.as_posix()
    return posix in gitignored or f"{posix}/" in gitignored


def _walk_directory(
    directory: Path,
    relative_directory: PurePosixPath,
    ignored: list[PurePosixPath],
    withheld: list[PurePosixPath],
    gitignored: frozenset[str],
) -> Iterator[tuple[Path, PurePosixPath]]:
    for child in sorted(directory.iterdir(), key=lambda item: item.name):
        relative_child = relative_directory / child.name
        if child.name in _IGNORED_DIRECTORY_NAMES or _is_ignored_file(child):
            ignored.append(relative_child)
            continue
        if _is_git_ignored(relative_child, gitignored):
            ignored.append(relative_child)
            continue
        if _is_withheld(relative_child):
            withheld.append(relative_child)
            continue
        _validate_public_component(child.name, relative_child)
        child_kind = _source_node_kind(child, relative_child)
        if child_kind is EntryKind.FILE:
            yield child, relative_child
        else:
            yield from _walk_directory(child, relative_child, ignored, withheld, gitignored)


def _files_for_entry(
    manifest: ImportManifest,
    entry: AllowEntry,
    ignored: list[PurePosixPath],
    withheld: list[PurePosixPath],
) -> Iterator[tuple[Path, PurePosixPath]]:
    relative_path = entry.path
    if _is_operator_state(relative_path):
        raise SafetyError(
            f"Operator state cannot be allowlisted directly: {relative_path.as_posix()}. "
            "The public repository owns its own example copy of this file."
        )
    _validate_public_component(relative_path.name, relative_path)
    source = _source_path(manifest.source_root, relative_path)
    actual_kind = _source_node_kind(source, relative_path)
    if actual_kind is not entry.kind:
        raise ManifestError(
            f"Allowlisted kind mismatch for {relative_path.as_posix()}: "
            f"manifest says {entry.kind.value}, source is {actual_kind.value}."
        )
    if actual_kind is EntryKind.FILE:
        if _is_ignored_file(source):
            raise SafetyError(f"Generated files cannot be allowlisted directly: {relative_path}")
        yield source, relative_path
        return
    gitignored = _collect_gitignored(manifest.source_root)
    yield from _walk_directory(source, relative_path, ignored, withheld, gitignored)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _destination_path(destination_root: Path, relative_path: PurePosixPath) -> Path:
    candidate = destination_root.joinpath(*relative_path.parts)
    if candidate.is_symlink():
        raise SafetyError(f"Destination symlinks are forbidden: {relative_path.as_posix()}")
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(destination_root):
        raise SafetyError(f"Destination escapes its root: {relative_path.as_posix()}")

    current = destination_root
    for component in relative_path.parts[:-1]:
        current /= component
        if current.is_symlink():
            raise SafetyError(f"Destination parent is a symlink: {relative_path.as_posix()}")
        if current.exists() and not current.is_dir():
            raise ConflictError(f"Destination parent is not a directory: {current}")
    return candidate


def _plan_file(
    manifest: ImportManifest,
    entry: AllowEntry,
    source: Path,
    relative_path: PurePosixPath,
) -> FileAction:
    destination = _destination_path(manifest.destination_root, relative_path)
    source_sha256 = _sha256_file(source)
    destination_sha256: str | None = None
    size_bytes = source.stat().st_size
    if not destination.exists():
        action_kind = ActionKind.CREATE
    elif not destination.is_file():
        raise ConflictError(f"Destination is not a regular file: {destination}")
    else:
        destination_sha256 = _sha256_file(destination)
        if destination_sha256 == source_sha256:
            action_kind = ActionKind.UNCHANGED
        elif entry.overwrite:
            action_kind = ActionKind.REPLACE
        else:
            raise ConflictError(
                f"Destination differs and overwrite is false: {relative_path.as_posix()}"
            )
    return FileAction(
        source=source,
        destination=destination,
        relative_path=relative_path,
        kind=action_kind,
        source_sha256=source_sha256,
        destination_sha256=destination_sha256,
        size_bytes=size_bytes,
    )


def build_plan(manifest: ImportManifest) -> ImportPlan:
    """Resolve every allowlisted file and reject the whole plan before writes."""

    ignored: list[PurePosixPath] = []
    withheld: list[PurePosixPath] = []
    actions: list[FileAction] = []
    for entry in manifest.entries:
        destination_entry = manifest.destination_root.joinpath(*entry.path.parts)
        if (
            entry.kind is EntryKind.DIRECTORY
            and destination_entry.exists()
            and (destination_entry.is_symlink() or not destination_entry.is_dir())
        ):
            raise ConflictError(
                f"Destination directory conflicts with an existing node: {entry.path}"
            )
        for source, relative_path in _files_for_entry(manifest, entry, ignored, withheld):
            actions.append(_plan_file(manifest, entry, source, relative_path))

    actions.sort(key=lambda action: action.relative_path.as_posix())
    ignored.sort(key=PurePosixPath.as_posix)
    withheld.sort(key=PurePosixPath.as_posix)
    return ImportPlan(
        manifest=manifest,
        actions=tuple(actions),
        ignored_paths=tuple(ignored),
        withheld_paths=tuple(withheld),
    )


def _copy_action(action: FileAction, destination_root: Path) -> None:
    if action.kind is ActionKind.UNCHANGED:
        return
    if _sha256_file(action.source) != action.source_sha256:
        raise ConflictError(f"Source changed after planning: {action.relative_path.as_posix()}")
    if action.kind is ActionKind.CREATE and action.destination.exists():
        raise ConflictError(
            f"Destination appeared after planning: {action.relative_path.as_posix()}"
        )
    if action.kind is ActionKind.REPLACE and (
        action.destination.is_symlink()
        or not action.destination.is_file()
        or _sha256_file(action.destination) != action.destination_sha256
    ):
        raise ConflictError(
            f"Destination changed after planning: {action.relative_path.as_posix()}"
        )

    action.destination.parent.mkdir(parents=True, exist_ok=True)
    _destination_path(destination_root, action.relative_path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{action.destination.name}.public-import-",
        dir=action.destination.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        shutil.copy2(action.source, temporary_path, follow_symlinks=False)
        if _sha256_file(temporary_path) != action.source_sha256:
            raise ConflictError(f"Copied bytes changed unexpectedly: {action.relative_path}")
        os.replace(temporary_path, action.destination)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def apply_plan(plan: ImportPlan) -> None:
    """Apply a previously validated plan without deleting destination files."""

    for action in plan.actions:
        _copy_action(action, plan.manifest.destination_root)


def render_plan(plan: ImportPlan, *, summary_only: bool = False) -> str:
    lines = [
        f"source: {plan.manifest.source_root}",
        f"destination: {plan.manifest.destination_root}",
    ]
    if not summary_only:
        lines.extend(
            f"{action.kind.value.upper():9} {action.relative_path.as_posix()}"
            for action in plan.actions
        )
        lines.extend(f"IGNORE    {path.as_posix()}" for path in plan.ignored_paths)
    # Withheld paths print even in summary mode. An ignored file is noise, but a
    # withheld one means the destination is keeping its own copy of something the
    # source also has, and that is the difference an operator has to be told about.
    lines.extend(
        f"WITHHELD  {path.as_posix()} ({withheld_reason(path)})" for path in plan.withheld_paths
    )
    lines.append(
        "summary: "
        f"create={plan.count(ActionKind.CREATE)} "
        f"replace={plan.count(ActionKind.REPLACE)} "
        f"unchanged={plan.count(ActionKind.UNCHANGED)} "
        f"ignored={len(plan.ignored_paths)} "
        f"withheld={len(plan.withheld_paths)} "
        f"bytes_to_write={plan.bytes_to_write}"
    )
    return "\n".join(lines)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or apply an allowlisted public snapshot import.",
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=Path("public_import.toml"),
        help="TOML manifest path (default: public_import.toml)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the validated plan. Without this flag, the command is read-only.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Suppress the per-file plan.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        plan = build_plan(manifest)
        print(render_plan(plan, summary_only=args.summary_only))
        if args.apply:
            apply_plan(plan)
            print("applied: public snapshot updated")
        else:
            print("dry-run: no files written; pass --apply to execute this plan")
    except SnapshotError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
