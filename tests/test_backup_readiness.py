# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
from pathlib import Path

from local_first_agent_os.backup_readiness import (
    MAX_BACKUP_AGE_SECONDS,
    BackupBlocked,
    BackupReady,
    check_backup_readiness,
)


def _complete(directory: Path, name: str, *, mtime: float) -> Path:
    backup_set = directory / name
    backup_set.mkdir(parents=True)
    (backup_set / "COMPLETE").touch()
    os.utime(backup_set, (mtime, mtime))
    return backup_set


def test_backup_readiness_requires_a_distinct_absolute_copy_target(tmp_path: Path) -> None:
    target_file = tmp_path / "target"
    target_file.write_text("relative\n", encoding="utf-8")

    result = check_backup_readiness(tmp_path / "backups", target_file, now=100.0)

    assert isinstance(result, BackupBlocked)
    assert "absolute directory distinct" in result.message


def test_backup_readiness_requires_the_newest_set_on_the_copy_target(tmp_path: Path) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    target_file = tmp_path / "target"
    target_file.write_text(f"{remote}\n", encoding="utf-8")
    remote.mkdir()
    _complete(local, "20260830T100000Z", mtime=100.0)

    result = check_backup_readiness(local, target_file, now=101.0, allow_same_device=True)

    assert isinstance(result, BackupBlocked)
    assert "no complete off-machine copy" in result.message


def test_backup_readiness_rejects_a_stale_pair(tmp_path: Path) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    target_file = tmp_path / "target"
    target_file.write_text(f"{remote}\n", encoding="utf-8")
    _complete(local, "20260830T100000Z", mtime=100.0)
    _complete(remote, "20260830T100000Z", mtime=100.0)

    result = check_backup_readiness(
        local,
        target_file,
        now=100.0 + MAX_BACKUP_AGE_SECONDS + 1,
        allow_same_device=True,
    )

    assert isinstance(result, BackupBlocked)
    assert "hour(s) old" in result.message


def test_backup_readiness_accepts_a_recent_atomic_pair(tmp_path: Path) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    target_file = tmp_path / "target"
    target_file.write_text(f"{remote}\n", encoding="utf-8")
    expected = _complete(local, "20260830T100000Z", mtime=100.0)
    copied = _complete(remote, "20260830T100000Z", mtime=100.0)

    result = check_backup_readiness(local, target_file, now=160.0, allow_same_device=True)

    assert result == BackupReady(expected, copied, 60)


def test_backup_readiness_refuses_a_same_filesystem_copy_without_explicit_sync(
    tmp_path: Path,
) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    remote.mkdir()
    target_file = tmp_path / "target"
    target_file.write_text(f"{remote}\n", encoding="utf-8")

    result = check_backup_readiness(local, target_file, now=160.0)

    assert isinstance(result, BackupBlocked)
    assert "same filesystem" in result.message
