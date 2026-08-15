# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which failures are the other provider's problem to solve.

A frontier run that dies because its credential expired is the same shape of
problem as one that dies because the quota ran out: this provider cannot
continue, the peer can, and the work is unchanged. Only the quota case was
wired, so the failure the start-time probe is best at catching was the one the
runtime could not route around when it happened *after* the probe had passed.
"""

from __future__ import annotations

import pytest

from local_first_agent_os.pow_wow.process import (
    infer_frontier_fallback_reason,
    warrants_provider_swap,
)
from local_first_agent_os.pow_wow.types import CommandRunCapture


def _capture(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandRunCapture:
    return CommandRunCapture(
        command="claude -p",
        cwd="/tmp",
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
    )


@pytest.mark.parametrize(
    "text",
    [
        "Error: authentication expired",
        "authentication invalid",
        "not authenticated",
        "HTTP 401",
        "401 Unauthorized",
        "invalid api key",
        "OAuth token expired",
    ],
    ids=lambda t: t[:28],
)
def test_a_dead_credential_is_a_reason_to_change_provider(text: str) -> None:
    assert infer_frontier_fallback_reason(_capture(stderr=text)) == "authentication_failed"
    assert warrants_provider_swap("authentication_failed") is True


def test_a_usage_limit_still_changes_provider() -> None:
    """Unchanged behaviour, asserted so adding the auth case cannot cost it."""

    assert (
        infer_frontier_fallback_reason(_capture(stdout="hit your session limit")) == "usage_limit"
    )
    assert warrants_provider_swap("usage_limit") is True


def test_a_timeout_falls_back_without_blaming_the_provider() -> None:
    """A slow run is not evidence this provider cannot serve.

    It still reaches the fallback, and the ledger records no replacement policy,
    because "try the other one" and "this one is unusable" are different claims.
    """

    assert infer_frontier_fallback_reason(_capture(exit_code=124)) == "timeout"
    assert warrants_provider_swap("timeout") is False


def test_ordinary_output_about_authorization_does_not_swap_providers() -> None:
    """The false positive worth designing against.

    An agent working on authorization code prints these words in the course of
    doing its job. `classify_failure` may call that an auth failure for
    labelling; here it would abandon a working provider mid-run, so the patterns
    are deliberately narrower than that function's.
    """

    output = "Added a test asserting the endpoint returns unauthorized for anonymous callers."

    assert infer_frontier_fallback_reason(_capture(stdout=output, exit_code=0)) is None


def test_a_clean_run_is_not_a_fallback() -> None:
    assert infer_frontier_fallback_reason(_capture(stdout="done", exit_code=0)) is None
    assert warrants_provider_swap(None) is False
