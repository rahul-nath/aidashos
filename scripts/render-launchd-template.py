#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import argparse
import os
from pathlib import Path


def _stable_bin_path(binary: Path) -> str:
    """Absolute, but without following symlinks.

    `Path.resolve()` dereferences, and for a Homebrew binary that turns the
    stable `/opt/homebrew/bin/uv` into the version-pinned real file underneath
    it, `/opt/homebrew/Cellar/uv/<version>/bin/uv`. The next `brew upgrade uv`
    deletes that Cellar directory and every plist rendered this way stops
    working, with launchd exiting 78 before the service writes a log line: the
    failure looks like silence rather than an error. Observed on 2026-08-14,
    where five services had been dead since a 0.10.10 -> 0.12.3 upgrade.
    """

    return os.path.abspath(binary)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a portable launchd plist template")
    parser.add_argument("template", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("repo", type=Path)
    parser.add_argument("uv", type=Path)
    args = parser.parse_args()
    rendered = (
        args.template.read_text(encoding="utf-8")
        .replace("__REPO_ROOT__", str(args.repo.resolve()))
        .replace("__HOME__", str(Path.home()))
        .replace("__UV_BIN__", _stable_bin_path(args.uv))
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
