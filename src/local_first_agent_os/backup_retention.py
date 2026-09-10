# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bound complete coordination backups without risking the last surviving copy."""

from __future__ import annotations

import argparse
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

BACKUP_SET_FORMAT: Final = "%Y%m%dT%H%M%SZ"
DEFAULT_RECENT_SETS: Final = 24
DEFAULT_DAILY_SETS: Final = 30
DEFAULT_MONTHLY_SETS: Final = 12


@dataclass(frozen=True)
class RetentionPolicy:
    recent_sets: int = DEFAULT_RECENT_SETS
    daily_sets: int = DEFAULT_DAILY_SETS
    monthly_sets: int = DEFAULT_MONTHLY_SETS

    def __post_init__(self) -> None:
        if min(self.recent_sets, self.daily_sets, self.monthly_sets) < 0:
            raise ValueError("backup retention counts cannot be negative")

    @property
    def maximum_sets(self) -> int:
        return self.recent_sets + self.daily_sets + self.monthly_sets


DEFAULT_RETENTION_POLICY: Final = RetentionPolicy()


@dataclass(frozen=True)
class BackupSet:
    name: str
    created_at: datetime


@dataclass(frozen=True)
class RetentionResult:
    kept: tuple[str, ...]
    pruned: tuple[str, ...]
    unpaired: tuple[str, ...]


def _backup_set(directory: Path) -> BackupSet | None:
    if directory.is_symlink() or not (directory / "COMPLETE").is_file():
        return None
    try:
        created_at = datetime.strptime(directory.name, BACKUP_SET_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
    return BackupSet(name=directory.name, created_at=created_at)


def _complete_sets(root: Path) -> dict[str, BackupSet]:
    if not root.is_dir():
        return {}
    return {
        backup_set.name: backup_set
        for path in root.iterdir()
        if path.is_dir() and (backup_set := _backup_set(path)) is not None
    }


def retained_set_names(
    backup_sets: tuple[BackupSet, ...],
    policy: RetentionPolicy = DEFAULT_RETENTION_POLICY,
) -> frozenset[str]:
    """Choose disjoint recent, daily, and monthly generations, newest first."""

    ordered = sorted(backup_sets, key=lambda item: item.created_at, reverse=True)
    recent = ordered[: policy.recent_sets]
    keep = {item.name for item in recent}
    recent_days = {item.created_at.date() for item in recent}

    daily_days: set[date] = set()
    daily: list[BackupSet] = []
    monthly_candidates: list[BackupSet] = []
    for item in ordered[policy.recent_sets :]:
        day = item.created_at.date()
        if day in recent_days or day in daily_days:
            continue
        if len(daily) < policy.daily_sets:
            daily.append(item)
            daily_days.add(day)
            keep.add(item.name)
        else:
            monthly_candidates.append(item)

    covered_months = {(item.created_at.year, item.created_at.month) for item in (*recent, *daily)}
    monthly_months: set[tuple[int, int]] = set()
    for item in monthly_candidates:
        month = (item.created_at.year, item.created_at.month)
        if month in covered_months or month in monthly_months:
            continue
        if len(monthly_months) == policy.monthly_sets:
            break
        monthly_months.add(month)
        keep.add(item.name)

    return frozenset(keep)


def apply_backup_retention(
    local_root: Path,
    copy_root: Path,
    policy: RetentionPolicy = DEFAULT_RETENTION_POLICY,
) -> RetentionResult:
    """Prune only complete pairs, external first, so a failure leaves a survivor."""

    local_root = local_root.expanduser().resolve()
    copy_root = copy_root.expanduser().resolve()
    if local_root == copy_root:
        raise ValueError("local and copy backup roots must be distinct")
    if not local_root.is_dir() or not copy_root.is_dir():
        raise ValueError("local and copy backup roots must already exist")

    local = _complete_sets(local_root)
    copied = _complete_sets(copy_root)
    paired_names = local.keys() & copied.keys()
    paired = tuple(local[name] for name in paired_names)
    keep = retained_set_names(paired, policy)
    prune = sorted(
        (item for item in paired if item.name not in keep),
        key=lambda item: item.created_at,
    )

    pruned: list[str] = []
    for item in prune:
        shutil.rmtree(copy_root / item.name)
        shutil.rmtree(local_root / item.name)
        pruned.append(item.name)

    return RetentionResult(
        kept=tuple(sorted(keep, reverse=True)),
        pruned=tuple(pruned),
        unpaired=tuple(sorted(local.keys() ^ copied.keys(), reverse=True)),
    )


def _retention_count(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-root", type=Path, required=True)
    parser.add_argument("--copy-root", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    policy = RetentionPolicy(
        recent_sets=_retention_count("LOCAL_AGENT_BACKUP_RECENT_SETS", DEFAULT_RECENT_SETS),
        daily_sets=_retention_count("LOCAL_AGENT_BACKUP_DAILY_SETS", DEFAULT_DAILY_SETS),
        monthly_sets=_retention_count("LOCAL_AGENT_BACKUP_MONTHLY_SETS", DEFAULT_MONTHLY_SETS),
    )
    result = apply_backup_retention(args.local_root, args.copy_root, policy)
    print(
        f"Backup retention: kept={len(result.kept)} pruned={len(result.pruned)} "
        f"unpaired={len(result.unpaired)} maximum={policy.maximum_sets}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
