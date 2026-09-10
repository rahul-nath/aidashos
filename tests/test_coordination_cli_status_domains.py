# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Any

import pytest

from local_first_agent_os.coordination import cli


@pytest.mark.parametrize("status", ("FAILED", "BLOCKED", "CANCELED"))
def test_milestone_failure_cli_preserves_milestone_status(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    received: dict[str, Any] = {}

    def fail(milestone_id: str, reason: str, *, status: str) -> dict[str, Any]:
        received.update(milestone_id=milestone_id, reason=reason, status=status)
        return {"ok": True}

    monkeypatch.setattr(cli, "fail_saga_milestone", fail)
    args = cli.build_parser().parse_args(
        ["fail_saga_milestone", "milestone-test", "test refusal", "--status", status]
    )

    assert cli.dispatch(args) == {"ok": True}
    assert received == {
        "milestone_id": "milestone-test",
        "reason": "test refusal",
        "status": status,
    }
