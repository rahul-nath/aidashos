# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Blobs no live transcript references are collectable; the rest are not.

The content-addressed store deduplicates, so it is bounded in the number of
distinct images an operator ever pastes and unbounded in time: compacting a
transcript rewrites away the reference and nothing ever removed the blob. These
tests pin the reachability argument that makes collecting them safe, and the
two guards that keep it from eating live data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_first_agent_os.lifecycle_maintenance import sweep_session_artifacts
from local_first_agent_os.session_handoff import (
    HandoffIntegrityError,
    sweep_unreferenced_artifacts,
)

OLD = 10_000.0  # seconds in the past, well beyond the default age guard


def _blob(artifacts: Path, digest: str, *, body: bytes = b"x" * 32, age: float = OLD) -> Path:
    path = artifacts / f"sha256-{digest}.png"
    path.write_bytes(body)
    import os
    import time

    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def _transcript(path: Path, digests: list[str]) -> None:
    rows = [{"content": f"artifact://sha256/{digest}"} for digest in digests]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "sess"
    (directory / "artifacts").mkdir(parents=True)
    return directory


def test_an_unreferenced_blob_is_reported_but_not_deleted_by_default(session_dir: Path) -> None:
    dead = _blob(session_dir / "artifacts", "a" * 64)
    _transcript(session_dir / "session.jsonl", [])

    result = sweep_unreferenced_artifacts(session_dir)

    assert result.unreferenced_blobs == 1
    assert result.reclaimed_bytes == 32
    assert result.deleted is False
    assert dead.exists(), "read-only by default; the argument gets looked at first"


def test_deleting_removes_only_the_unreachable(session_dir: Path) -> None:
    live = _blob(session_dir / "artifacts", "b" * 64)
    dead = _blob(session_dir / "artifacts", "c" * 64)
    _transcript(session_dir / "session.jsonl", ["b" * 64])

    result = sweep_unreferenced_artifacts(session_dir, delete=True)

    assert result.reachable_digests == 1
    assert result.unreferenced_blobs == 1
    assert live.exists()
    assert not dead.exists()


def test_a_backup_generation_is_a_root(session_dir: Path) -> None:
    """Backups exist to be restored, so collecting what they reference would
    make restoring one produce dangling references."""

    only_in_backup = _blob(session_dir / "artifacts", "d" * 64)
    _transcript(session_dir / "session.jsonl", [])
    backups = session_dir / "backups"
    backups.mkdir()
    _transcript(backups / "session-g0.jsonl", ["d" * 64])

    result = sweep_unreferenced_artifacts(session_dir, delete=True)

    assert result.scanned_transcripts == 2
    assert result.unreferenced_blobs == 0
    assert only_in_backup.exists()


def test_a_recent_blob_is_spared(session_dir: Path) -> None:
    """A sweep racing an in-progress externalization must not win."""

    fresh = _blob(session_dir / "artifacts", "e" * 64, age=0.0)
    _transcript(session_dir / "session.jsonl", [])

    result = sweep_unreferenced_artifacts(session_dir, delete=True)

    assert result.skipped_recent == 1
    assert result.unreferenced_blobs == 0
    assert fresh.exists()


def test_an_unreadable_transcript_refuses_the_whole_sweep(session_dir: Path) -> None:
    """Under-counting roots would delete live blobs, so it fails instead."""

    _blob(session_dir / "artifacts", "f" * 64)
    (session_dir / "session.jsonl").write_bytes(b"\xff\xfe not utf-8")

    with pytest.raises(HandoffIntegrityError, match="Could not read transcript"):
        sweep_unreferenced_artifacts(session_dir, delete=True)


def test_a_session_without_a_store_is_skipped(tmp_path: Path) -> None:
    result = sweep_unreferenced_artifacts(tmp_path / "nothing-here")

    assert result.scanned_transcripts == 0
    assert result.unreferenced_blobs == 0


def test_the_janitor_degrades_one_session_rather_than_the_run(tmp_path: Path) -> None:
    """One unreadable session is not a reason to skip bounding everything else."""

    export_root = tmp_path / "exports"
    healthy = export_root / "good"
    (healthy / "artifacts").mkdir(parents=True)
    _blob(healthy / "artifacts", "1" * 64)
    _transcript(healthy / "session.jsonl", [])

    broken = export_root / "bad"
    (broken / "artifacts").mkdir(parents=True)
    (broken / "session.jsonl").write_bytes(b"\xff\xfe")

    sweeps = sweep_session_artifacts(export_root, delete=False)

    by_dir = {Path(entry["session_dir"]).name: entry for entry in sweeps}
    assert by_dir["good"]["unreferenced_blobs"] == 1
    assert by_dir["bad"]["status"] == "DEGRADED"


def test_sweeping_is_off_by_default_in_settings() -> None:
    from local_first_agent_os.settings import Settings

    assert Settings().lifecycle_sweep_session_artifacts is False
