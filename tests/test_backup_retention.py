# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from local_first_agent_os.backup_retention import (
    BackupSet,
    RetentionPolicy,
    apply_backup_retention,
    retained_set_names,
)


def _name(created_at: datetime) -> str:
    return created_at.strftime("%Y%m%dT%H%M%SZ")


def _complete(root: Path, created_at: datetime) -> str:
    name = _name(created_at)
    backup_set = root / name
    backup_set.mkdir(parents=True)
    (backup_set / "COMPLETE").touch()
    return name


def test_default_policy_keeps_disjoint_recent_daily_and_monthly_tiers() -> None:
    newest = datetime(2026, 8, 30, 18, tzinfo=UTC)
    generations = tuple(
        BackupSet(
            name=_name(newest - timedelta(hours=6 * index)),
            created_at=newest - timedelta(hours=6 * index),
        )
        for index in range(1_600)
    )

    kept = retained_set_names(generations)
    by_name = {item.name: item for item in generations}
    ordered = sorted(
        (by_name[name] for name in kept),
        key=lambda item: item.created_at,
        reverse=True,
    )

    assert len(kept) == 66
    assert {item.name for item in ordered[:24]} == {item.name for item in generations[:24]}
    assert len({item.created_at.date() for item in ordered[24:54]}) == 30
    assert len({(item.created_at.year, item.created_at.month) for item in ordered[54:]}) == 12


def test_retention_prunes_only_complete_pairs_and_preserves_an_unpaired_survivor(
    tmp_path: Path,
) -> None:
    local = tmp_path / "local"
    copied = tmp_path / "copied"
    local.mkdir()
    copied.mkdir()
    newest = datetime(2026, 8, 30, 18, tzinfo=UTC)
    names = [
        (_complete(local, newest - timedelta(hours=6 * index)), newest - timedelta(hours=6 * index))
        for index in range(4)
    ]
    for name, created_at in names[:3]:
        assert name == _complete(copied, created_at)

    result = apply_backup_retention(
        local,
        copied,
        RetentionPolicy(recent_sets=1, daily_sets=0, monthly_sets=0),
    )

    assert result.kept == (names[0][0],)
    assert result.pruned == (names[2][0], names[1][0])
    assert result.unpaired == (names[3][0],)
    assert (local / names[3][0] / "COMPLETE").is_file()
    assert not (copied / names[3][0]).exists()


def test_retention_ignores_failed_partial_and_unrecognized_directories(tmp_path: Path) -> None:
    local = tmp_path / "local"
    copied = tmp_path / "copied"
    local.mkdir()
    copied.mkdir()
    for root in (local, copied):
        (root / ".20260830T180000Z.partial").mkdir()
        (root / "20260830T120000Z.failed").mkdir()
        (root / "notes").mkdir()

    result = apply_backup_retention(local, copied, RetentionPolicy(0, 0, 0))

    assert result.kept == ()
    assert result.pruned == ()
    assert result.unpaired == ()
    assert {item.name for item in local.iterdir()} == {
        ".20260830T180000Z.partial",
        "20260830T120000Z.failed",
        "notes",
    }


def test_retention_refuses_the_same_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be distinct"):
        apply_backup_retention(tmp_path, tmp_path)
