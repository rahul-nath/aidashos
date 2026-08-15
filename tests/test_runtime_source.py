# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One answer to "which code is running", for everything that publishes it.

`pi-daemon` puts this on `/health` and `start-agent-runtime.sh` compares it
against a checkout's HEAD to decide whether a resident daemon is serving stale
code. Three modules used to compute it separately and had already drifted on
what a failed `git` means, which turns into a staleness verdict that is not
about staleness.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from local_first_agent_os import runtime_source
from local_first_agent_os.runtime_source import (
    RUNTIME_REVISION_ENV_VAR,
    revision_of,
    runtime_checkout,
    runtime_revision,
)


def test_the_checkout_is_the_repository_this_code_was_loaded_from() -> None:
    checkout = runtime_checkout()

    assert (checkout / "src" / "local_first_agent_os" / "runtime_source.py").is_file()
    assert (checkout / ".git").exists()


def test_the_environment_override_wins_over_the_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`start-agent-runtime.sh` names the revision it asked for; believe it."""

    monkeypatch.setenv(RUNTIME_REVISION_ENV_VAR, "the-revision-the-operator-asked-for")

    assert runtime_revision() == "the-revision-the-operator-asked-for"


def test_an_empty_override_is_not_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported-but-empty variable is absence, not a revision named ''."""

    monkeypatch.setenv(RUNTIME_REVISION_ENV_VAR, "   ")
    monkeypatch.setattr(runtime_source, "revision_of", lambda _checkout: "from-the-checkout")

    assert runtime_revision() == "from-the-checkout"


def test_without_an_override_it_reads_the_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """launchd does not inherit the start script's environment, so this path
    is the one the installed pi-daemon actually takes."""

    monkeypatch.delenv(RUNTIME_REVISION_ENV_VAR, raising=False)
    monkeypatch.setattr(runtime_source, "revision_of", lambda _checkout: "module-checkout-revision")

    assert runtime_revision() == "module-checkout-revision"


class TestRevisionOf:
    """An unreadable revision is None, and never a partial or stale answer.

    The start script treats None as "cannot verify" and says so. Returning a
    stale value, or raising where callers expect a value, would both turn into
    a claim about staleness that nothing checked.
    """

    def test_it_reads_head_of_the_given_checkout(self) -> None:
        expected = subprocess.run(
            ["git", "-C", str(runtime_checkout()), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        assert revision_of(runtime_checkout()) == expected

    def test_a_directory_that_is_not_a_repository_has_no_revision(self, tmp_path: Path) -> None:
        assert revision_of(tmp_path) is None

    def test_a_missing_git_is_not_an_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _no_git(*_args: object, **_kwargs: object) -> object:
            raise OSError("git not found")

        monkeypatch.setattr(runtime_source.subprocess, "run", _no_git)

        assert revision_of(runtime_checkout()) is None

    def test_a_slow_git_is_not_an_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _hangs(*_args: object, **_kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd="git", timeout=5)

        monkeypatch.setattr(runtime_source.subprocess, "run", _hangs)

        assert revision_of(runtime_checkout()) is None
