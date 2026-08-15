#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import argparse
import os
import plistlib
import shlex
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("platform", choices=("launchd", "systemd"))
    parser.add_argument("repo", type=Path)
    parser.add_argument("uv", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    # Absolute but not dereferenced. `.resolve()` would turn the stable
    # `/opt/homebrew/bin/uv` into the version-pinned `/opt/homebrew/Cellar/uv/
    # <version>/bin/uv` underneath it, and the next `brew upgrade uv` deletes
    # that path. The service then fails to exec with launchd exiting 78 and no
    # log line written, which reads as silence rather than as an error. Same
    # reasoning as `_stable_bin_path` in `render-launchd-template.py`.
    uv = Path(os.path.abspath(args.uv))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Settings owns dotenv parsing from the configured working directory.
    # A dotenv file is not a shell script: sourcing JSON array values through
    # Bash strips their inner quotes before pydantic-settings can parse them.
    shell = f"cd {shlex.quote(str(repo))} && exec {shlex.quote(str(uv))} run pi-daemon"
    log_dir = Path.home() / ".local-agent" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if args.platform == "launchd":
        payload = {
            "Label": "com.rahul.local-first-agent.pi-daemon",
            "ProgramArguments": ["/bin/bash", "-lc", shell],
            "WorkingDirectory": str(repo),
            "RunAtLoad": True,
            "KeepAlive": False,
            "StandardOutPath": str(log_dir / "pi-daemon.out.log"),
            "StandardErrorPath": str(log_dir / "pi-daemon.err.log"),
        }
        with args.output.open("wb") as handle:
            plistlib.dump(payload, handle)
        return 0
    unit = "\n".join(
        (
            "[Unit]",
            "Description=Local First Agent OS Pi daemon",
            "After=network.target docker.service",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={repo}",
            f"ExecStart=/bin/bash -lc {shlex.quote(shell)}",
            "Restart=on-failure",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        )
    )
    args.output.write_text(unit, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
