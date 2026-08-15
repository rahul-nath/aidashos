# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The llama startup failure message must name the log that actually exists.

Under launchd, llama-server's output goes to the plist's StandardErrorPath, not
to the repo-relative log the script starts with. An operator reading a startup
failure is debugging blind if the message names the wrong file, so both the
resolution and the messages that consume it are pinned here.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "start-agent-runtime.sh"
PLIST_BUDDY = Path("/usr/libexec/PlistBuddy")


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_failure_messages_name_the_resolved_log_not_a_fixed_path() -> None:
    """Both exits are covered: the readiness timeout and the gemma4 proof."""

    text = _script_text()

    assert 'echo "llama-server did not become ready. Check $LLAMA_LOG" >&2' in text
    assert "Check $LLAMA_LOG, then retry: pi /start /gemma4" in text


def test_the_hardcoded_log_path_survives_only_where_it_is_written() -> None:
    """The literal belongs to the default and the redirect that fills it.

    Anywhere else it is a message naming a file this run may never write.
    """

    occurrences = [
        line.strip()
        for line in _script_text().splitlines()
        if ".local_agent/logs/llama-router.log" in line
    ]

    assert all(
        line.startswith("LLAMA_LOG=") or line.startswith("nohup ") or line.startswith("rm -f")
        for line in occurrences
    ), occurrences


def test_the_repo_relative_log_remains_the_default() -> None:
    """A non-launchd run still has a real log to name."""

    text = _script_text()

    assert 'LLAMA_LOG=".local_agent/logs/llama-router.log"' in text
    # The nohup branch that starts llama itself must write where the default points.
    assert "nohup ./scripts/start-llama.sh >.local_agent/logs/llama-router.log" in text


@pytest.mark.skipif(not PLIST_BUDDY.exists(), reason="PlistBuddy is macOS only")
def test_plistbuddy_reads_standard_error_path_from_a_real_plist(tmp_path: Path) -> None:
    """Integration: the exact invocation the script uses, against a real plist.

    Pins the key name and the -c form. A typo in either fails silently in the
    script, because the whole call is swallowed by `|| echo "$LLAMA_LOG"`.
    """

    plist_path = tmp_path / "com.rahul.local-first-agent.llama.plist"
    expected = "/Users/somebody/Library/Logs/llama.err.log"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.rahul.local-first-agent.llama",
                "StandardErrorPath": expected,
                "StandardOutPath": "/Users/somebody/Library/Logs/llama.out.log",
            }
        )
    )

    result = subprocess.run(
        [str(PLIST_BUDDY), "-c", "Print :StandardErrorPath", str(plist_path)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == expected


@pytest.mark.skipif(not PLIST_BUDDY.exists(), reason="PlistBuddy is macOS only")
def test_missing_standard_error_path_fails_so_the_default_is_kept(tmp_path: Path) -> None:
    """The `|| echo` fallback is only correct if the lookup really fails here."""

    plist_path = tmp_path / "no-stderr.plist"
    plist_path.write_bytes(plistlib.dumps({"Label": "com.rahul.local-first-agent.llama"}))

    result = subprocess.run(
        [str(PLIST_BUDDY), "-c", "Print :StandardErrorPath", str(plist_path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
