# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A rendered service must not pin the interpreter to a version that moves.

Homebrew installs `uv` as a stable symlink at `/opt/homebrew/bin/uv` pointing
into a version-pinned directory, `/opt/homebrew/Cellar/uv/<version>/bin/uv`.
Both renderers used `Path.resolve()`, which dereferences, so every plist named
the Cellar path. `brew upgrade uv` then deleted the directory those plists
pointed at.

The resulting failure is the reason this is a test rather than a comment.
launchd cannot exec a missing program, so it exits 78 before the service writes
anything, and the logs simply stop. On 2026-08-14 five services had been dead
that way since a 0.10.10 -> 0.12.3 upgrade, and `start-agent-runtime.sh` still
exited 0 because it swallows bootstrap failures.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "scripts"


def _homebrew_shaped_uv(tmp_path: Path) -> tuple[Path, Path]:
    """A stable `bin/uv` symlink over a version-pinned real file."""

    cellar = tmp_path / "Cellar" / "uv" / "0.10.10" / "bin"
    cellar.mkdir(parents=True)
    real = cellar / "uv"
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "uv"
    link.symlink_to(real)
    return link, real


def test_launchd_template_keeps_the_symlink_not_its_target(tmp_path: Path) -> None:
    link, real = _homebrew_shaped_uv(tmp_path)
    template = tmp_path / "template.plist"
    template.write_text("__UV_BIN__|__REPO_ROOT__", encoding="utf-8")
    output = tmp_path / "out.plist"

    subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "render-launchd-template.py"),
            str(template),
            str(output),
            str(tmp_path),
            str(link),
        ],
        check=True,
    )

    rendered = output.read_text(encoding="utf-8")
    assert str(link) in rendered
    assert str(real) not in rendered
    assert "Cellar" not in rendered


def test_pi_daemon_service_keeps_the_symlink_not_its_target(tmp_path: Path) -> None:
    link, real = _homebrew_shaped_uv(tmp_path)
    output = tmp_path / "pi.plist"

    subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "render-pi-daemon-service.py"),
            "launchd",
            str(tmp_path),
            str(link),
            str(output),
        ],
        check=True,
    )

    payload = plistlib.loads(output.read_bytes())
    program = " ".join(payload["ProgramArguments"])
    assert str(link) in program
    assert str(real) not in program
    assert "Cellar" not in program
