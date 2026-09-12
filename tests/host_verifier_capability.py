# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Explicit prerequisites for tests that provision a trusted host verification gate."""

from __future__ import annotations

import os
import socket
import sys

import pytest

from local_first_agent_os.native_verification_broker import authenticated_contained_client
from local_first_agent_os.uid_verifier_client import (
    HELPER_SOCKET,
    UidVerifierUnavailable,
    _root_peer,
)


def require_host_uid_verifier() -> None:
    """Do not confuse child execution authority with operator-only gate ownership."""
    if authenticated_contained_client():
        pytest.skip(
            "trusted host gate provisioning is unavailable to an authenticated contained client"
        )
    try:
        if sys.platform != "darwin":
            raise UidVerifierUnavailable("native UID verifier qualification requires macOS")
        # Opening the transport creates no UID lease and grants no execution authority.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.5)
            connection.connect(str(HELPER_SOCKET))
            _root_peer(connection)
    except (OSError, UidVerifierUnavailable) as error:
        reason = "qualified native UID verifier helper is required: " + str(error)
        if os.environ.get("AIDASHOS_REQUIRE_UID_VERIFIER") == "1":
            pytest.fail(reason, pytrace=False)
        pytest.skip(reason)
