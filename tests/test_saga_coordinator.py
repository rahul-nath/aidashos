# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import pytest

from local_first_agent_os.coordination import saga_coordinator


def test_ambiguity_projection_uses_the_in_process_ledger_owner(monkeypatch) -> None:
    monkeypatch.setattr(
        saga_coordinator,
        "check_ambiguity",
        lambda _gawd_doc_id: {
            "ok": True,
            "scores": {
                "goal_clarity": 0.9,
                "constraints_clarity": 0.8,
                "success_criteria_clarity": 0.85,
                "unresolved_critical": 0,
            },
            "ready_to_execute": True,
            "passes": {"goal_clarity": True},
        },
    )

    result = saga_coordinator.check_ambiguity_heuristic("gawd-1")

    assert result.gawd_doc_id == "gawd-1"
    assert result.ready_to_execute is True
    assert result.unresolved_critical == 0


def test_stagnation_projection_fails_closed_when_the_ledger_refuses(monkeypatch) -> None:
    monkeypatch.setattr(
        saga_coordinator,
        "inspect_stagnation",
        lambda _saga_id: {"ok": False, "error": "saga_not_found"},
    )

    with pytest.raises(RuntimeError, match="saga_not_found"):
        saga_coordinator.check_stagnation("missing")
