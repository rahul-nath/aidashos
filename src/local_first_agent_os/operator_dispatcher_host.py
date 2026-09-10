# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Explicit local service bootstrap for the operator-owned dispatcher process.

This is not an HTTP/MCP operation and no public command calls it. Only the
configured host service loads the protected credential. Its trusted ledger
transport retains that credential; command and agent subprocesses strip it.
"""

from __future__ import annotations

import argparse
import math
import os
from collections.abc import Sequence
from pathlib import Path

from .operator_credentials import read_host_operator_credential
from .operator_identity import OPERATOR_TOKEN_ENV, operator_token_file, verify_operator_actor


def provision_dispatcher_host() -> None:
    """Require the explicit protected local credential before the loop can claim."""
    path = operator_token_file()
    credential = read_host_operator_credential(path)
    os.environ[OPERATOR_TOKEN_ENV] = credential
    try:
        verify_operator_actor("ledger-dispatcher-host")
    except BaseException:
        os.environ.pop(OPERATOR_TOKEN_ENV, None)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    from .coordination.cli import main as coordination_main
    from .coordination.contracts import CoordinationCommandName

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    if not args.root.is_absolute() or not args.root.is_dir():
        parser.error("--root must be an existing absolute repository path")
    if not math.isfinite(args.interval_seconds) or args.interval_seconds <= 0:
        parser.error("--interval-seconds must be finite and positive")
    provision_dispatcher_host()
    try:
        return coordination_main(
            [
                "--root",
                str(args.root),
                CoordinationCommandName.RUN_LEDGER_DISPATCHER.value,
                "--interval-seconds",
                str(args.interval_seconds),
            ]
        )
    finally:
        os.environ.pop(OPERATOR_TOKEN_ENV, None)


if __name__ == "__main__":
    raise SystemExit(main())
