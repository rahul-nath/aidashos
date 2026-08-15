# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The machine-readiness question, answered over MCP.

`scripts/first-run-check.sh` is the single implementation of "can this machine
run a governed task, and what exactly is missing". Shell callers run it
directly. This module exists for the callers that cannot: an MCP client with no
shell of its own (a desktop assistant, a web UI) can read the ledger but could
not ask the one question every onboarding conversation starts with. It adapts
the script to the MCP transport without restating any of its checks, so there
is still exactly one place the readiness facts are written down.

Read-only by inheritance: the script it shells to starts, stops, installs, and
writes nothing, and the optional frontier probe (the script's one paid flag) is
deliberately not exposed here.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

from .store import repo_root

_ANSI_SEQUENCE = re.compile(r"\x1b\[[0-9;]*m")

# first-run-check inspects the toolchain, the ledger, models on disk, and the
# frontier CLIs; none of that should take anywhere near this long. The bound
# exists so a wedged subprocess surfaces as an error instead of a hung tool.
_TIMEOUT_SECONDS = 300


def run_first_run_check() -> dict[str, Any]:
    """Report whether this machine can run a governed task, and what is missing.

    Every blocked line in the report carries the command that fixes it, so the
    payload is directly actionable without consulting anything else.
    """

    root = repo_root()
    script = root / "scripts" / "first-run-check.sh"
    if not script.is_file():
        return {
            "ok": False,
            "error": "MissingScript",
            "message": f"{script} does not exist; is the server rooted at a checkout?",
        }
    try:
        completed = subprocess.run(
            ["bash", str(script)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "Timeout",
            "message": f"first-run-check did not finish within {_TIMEOUT_SECONDS}s",
        }
    report = _ANSI_SEQUENCE.sub("", completed.stdout)
    if completed.stderr.strip():
        report = f"{report}\n{_ANSI_SEQUENCE.sub('', completed.stderr)}"
    return {
        "ok": True,
        "ready": completed.returncode == 0,
        "exit_code": completed.returncode,
        "report": report.strip(),
    }
