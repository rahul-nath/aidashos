# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The session-handoff module has callers, and these are them.

`session_handoff.py` was 1148 lines that nothing outside its own test imported:
no directive, no workflow, no CLI command, no route. Finished code with no call
path is worse than missing code, because from the outside the two look the same
and the design document keeps claiming the feature works.

These tests pin the wiring rather than the pipeline. The pipeline's own behavior
is covered by `test_session_handoff.py`; what is asserted here is that each
command exists, reaches the function it adapts, and returns its result.
"""

from __future__ import annotations

import json
import struct
import zlib
from base64 import b64encode
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_first_agent_os.cli import app

runner = CliRunner()


def _one_pixel_png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\xff\x00\xff"))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def raw_transcript(tmp_path: Path) -> Path:
    uri = "data:image/png;base64," + b64encode(_one_pixel_png()).decode("ascii")
    rows = [
        {"role": "user", "content": [{"type": "input_text", "text": "look at this"}]},
        {"role": "user", "content": [{"type": "input_image", "image_url": uri}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "one pixel"}]},
    ]
    path = tmp_path / "raw.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_export_then_verify_round_trips(raw_transcript: Path, tmp_path: Path) -> None:
    export = runner.invoke(
        app, ["handoff-export", "demo", str(raw_transcript), str(tmp_path / "out")]
    )
    assert export.exit_code == 0, export.output
    exported = json.loads(export.output)
    assert exported["artifacts"] == 1
    assert exported["rewritten_bytes"] < exported["original_bytes"]

    bundle_dir = Path(exported["bundle_dir"])
    assert "base64" not in (bundle_dir / "handoff.jsonl").read_text(encoding="utf-8")

    verify = runner.invoke(app, ["handoff-verify", str(bundle_dir)])
    assert verify.exit_code == 0, verify.output
    verified = json.loads(verify.output)
    assert verified == {
        "ok": True,
        "session_id": "demo",
        "artifacts": 1,
        "image_references": 1,
    }


def test_verify_fails_loudly_on_a_broken_bundle(raw_transcript: Path, tmp_path: Path) -> None:
    """A non-zero exit, because this command exists to be trusted in a script."""

    export = runner.invoke(
        app, ["handoff-export", "demo", str(raw_transcript), str(tmp_path / "out")]
    )
    bundle_dir = Path(json.loads(export.output)["bundle_dir"])
    for blob in (bundle_dir / "artifacts").rglob("*"):
        if blob.is_file():
            blob.write_bytes(b"tampered")

    verify = runner.invoke(app, ["handoff-verify", str(bundle_dir)])

    assert verify.exit_code == 1
    assert json.loads(verify.output)["ok"] is False


def test_init_hydrates_context_and_enables_the_resolver(
    raw_transcript: Path, tmp_path: Path
) -> None:
    result = runner.invoke(
        app, ["handoff-init", "demo", str(raw_transcript), str(tmp_path / "ws")]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source_session_id"] == "demo"
    assert payload["artifact_refs"] == 1
    assert payload["images"] == 1
    assert payload["token_count"] > 0
    # The receiving harness needs this registered before the first model turn.
    assert payload["enabled_tools"] == ["resolve_artifact"]


def test_externalize_replaces_in_place_and_keeps_a_backup(
    raw_transcript: Path, tmp_path: Path
) -> None:
    session = tmp_path / "session.jsonl"
    session.write_bytes(raw_transcript.read_bytes())
    original = session.read_text(encoding="utf-8")

    result = runner.invoke(app, ["handoff-externalize", str(session)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["image_count"] == 1
    assert payload["rewritten_bytes"] < payload["original_bytes"]
    assert "base64" not in session.read_text(encoding="utf-8")
    # The prior generation survives, which is what makes this promotion safe.
    assert Path(payload["backup_path"]).read_text(encoding="utf-8") == original
