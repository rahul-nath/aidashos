# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only proof that the coordination ledger has a recent second copy."""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

MAX_BACKUP_AGE_SECONDS: Final = 7 * 60 * 60
"""One backup interval plus one hour for a delayed launchd job."""

BACKUP_FIX: Final = "./scripts/backup-coordination-postgres.sh"


@dataclass(frozen=True)
class BackupReady:
    backup_set: Path
    copy_set: Path
    age_seconds: int

    @property
    def message(self) -> str:
        return f"coordination backup is {self.age_seconds // 60} minute(s) old"


@dataclass(frozen=True)
class BackupBlocked:
    message: str
    fix: str = BACKUP_FIX


type BackupReadiness = BackupReady | BackupBlocked


def _complete_sets(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(marker.parent for marker in directory.glob("*/COMPLETE") if marker.is_file())
    )


def _existing_ancestor(path: Path) -> Path:
    candidate = path.expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def check_backup_readiness(
    backup_dir: Path,
    copy_target_file: Path,
    *,
    now: float | None = None,
    max_age_seconds: int = MAX_BACKUP_AGE_SECONDS,
    allow_same_device: bool = False,
) -> BackupReadiness:
    """Return evidence for the newest atomic set or a single fixing action."""

    if not copy_target_file.is_file():
        return BackupBlocked(
            f"backup copy target is not configured at {copy_target_file}",
            f"write an absolute off-machine directory path to {copy_target_file}",
        )
    raw_target = copy_target_file.read_text(encoding="utf-8").splitlines()
    target = Path(raw_target[0]).expanduser() if raw_target else Path()
    if not target.is_absolute() or target.resolve() == backup_dir.expanduser().resolve():
        return BackupBlocked(
            "backup copy target must be an absolute directory distinct from the local backup",
            f"write an absolute off-machine directory path to {copy_target_file}",
        )
    if not target.is_dir():
        return BackupBlocked(
            f"backup copy target is not mounted or missing: {target}",
            f"mount or create the independently synchronized directory {target}",
        )
    local_device = _existing_ancestor(backup_dir).stat().st_dev
    if not allow_same_device and target.stat().st_dev == local_device:
        return BackupBlocked(
            "backup copy target is on the same filesystem as the local backup",
            "mount an external/network target, or set LOCAL_AGENT_BACKUP_ALLOW_SAME_DEVICE=true "
            "only for an independently synchronized directory",
        )

    complete = _complete_sets(backup_dir.expanduser())
    if not complete:
        return BackupBlocked(f"no complete coordination backup exists under {backup_dir}")
    newest = complete[-1]
    copied = target / newest.name
    if not (copied / "COMPLETE").is_file():
        return BackupBlocked(f"newest backup {newest.name} has no complete off-machine copy")

    age = max(0, int((time.time() if now is None else now) - newest.stat().st_mtime))
    if age > max_age_seconds:
        return BackupBlocked(f"newest coordination backup is {age // 3600} hour(s) old")
    return BackupReady(backup_set=newest, copy_set=copied, age_seconds=age)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "LOCAL_AGENT_BACKUP_DIR",
                "~/.local-agent/backups/postgres",
            )
        ).expanduser(),
    )
    parser.add_argument(
        "--copy-target-file",
        type=Path,
        default=Path(
            os.environ.get(
                "LOCAL_AGENT_BACKUP_COPY_TARGET_FILE",
                "~/.local-agent/backup-copy-target",
            )
        ).expanduser(),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = check_backup_readiness(
        args.backup_dir,
        args.copy_target_file,
        allow_same_device=os.environ.get("LOCAL_AGENT_BACKUP_ALLOW_SAME_DEVICE") == "true",
    )
    if isinstance(result, BackupReady):
        print(f"ready\t{result.message}\t")
        return 0
    print(f"blocked\t{result.message}\t{result.fix}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BACKUP_FIX",
    "MAX_BACKUP_AGE_SECONDS",
    "BackupBlocked",
    "BackupReadiness",
    "BackupReady",
    "check_backup_readiness",
]
