#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify the committed OpenAPI schema against the application, or rewrite it.

The web client's TypeScript types are generated from this file rather than
transcribed from the Python models. That only works if the file is stable across
runs and machines, so the output is sorted, newline-terminated, and produced
without touching a real database: `create_app()` builds a runtime, and a scratch
SQLite URL is enough for it to describe itself.

Checking is the default and writing takes `--write`, because the two differ in
what they cost when invoked by mistake. A check that was meant to be a write
prints a diagnosis and changes nothing. A write that was meant to be a check
silently overwrites the committed schema with whatever the working tree
currently describes, which is exactly how a drift this file exists to catch gets
laundered into "no changes" instead. The safe half is the one you get for free.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = _REPO_ROOT / "web" / "openapi.json"


def _isolate_runtime_environment() -> None:
    """Point the app at throwaway state so describing it needs no live services.

    The schema is a property of the code, not of any deployment. Reading a
    developer's `.env` here would make the committed artifact depend on whose
    machine ran the dump.
    """

    scratch = Path(tempfile.mkdtemp(prefix="openapi-dump-"))
    os.environ["LOCAL_AGENT_DATABASE_URL"] = f"sqlite:///{scratch / 'openapi.sqlite3'}"
    os.environ["LOCAL_AGENT_COORDINATION_BACKEND"] = "postgres"
    os.environ["AGENT_COORDINATION_ROOT"] = str(scratch)
    os.environ["LOCAL_AGENT_ARTIFACT_ROOT"] = str(scratch / "artifacts")
    os.environ["LOCAL_AGENT_SPOOL_DIR"] = str(scratch / "spool")
    os.environ.setdefault("LOCAL_AGENT_USE_DBOS", "false")
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")


def render_schema() -> str:
    _isolate_runtime_environment()
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from local_first_agent_os.api import create_app

    schema = create_app().openapi()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write",
        action="store_true",
        help="overwrite the committed schema with a fresh dump",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="the default: fail when the committed schema differs from a fresh dump",
    )
    args = parser.parse_args(argv)

    rendered = render_schema()
    if not args.write:
        if not args.output.exists():
            print(
                f"missing {args.output}; run scripts/dump_openapi.py --write",
                file=sys.stderr,
            )
            return 1
        if args.output.read_text(encoding="utf-8") != rendered:
            print(
                f"{args.output} is stale; run scripts/dump_openapi.py --write and "
                "regenerate the TypeScript types",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output} matches the application schema")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
