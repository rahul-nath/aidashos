# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Declare host integration scope without turning arbitrary runtime errors into skips."""

from __future__ import annotations

import os

import pytest

from local_first_agent_os.native_verification_broker import authenticated_contained_client


def require_uncontained_scope(*, reason: str, required_flag: str) -> None:
    """An authenticated child cannot provide host evidence; other prerequisites still run."""
    if not authenticated_contained_client():
        return
    if os.environ.get(required_flag) == "1":
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)
