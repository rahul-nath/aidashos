# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import host_test_scope
import pytest


@pytest.mark.parametrize(
    ("contained", "required", "refusal"),
    [
        (False, True, None),
        (True, False, pytest.skip.Exception),
        (True, True, pytest.fail.Exception),
    ],
)
def test_host_evidence_requirement_preserves_the_authenticated_scope(
    monkeypatch, contained, required, refusal
) -> None:
    monkeypatch.setattr(host_test_scope, "authenticated_contained_client", lambda: contained)
    monkeypatch.setenv("REQUIRE_THIS_HOST_TEST", "1" if required else "0")
    arguments = {
        "reason": "actual host capability required",
        "required_flag": "REQUIRE_THIS_HOST_TEST",
    }
    if refusal is None:
        host_test_scope.require_uncontained_scope(**arguments)
    else:
        with pytest.raises(refusal, match="actual host capability required"):
            host_test_scope.require_uncontained_scope(**arguments)
