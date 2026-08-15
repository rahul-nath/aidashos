# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The status legend is a census of the enums, not a sample.

The cockpit renders four status vocabularies, and the legend exists so no token
reaches the operator without its meaning and their next move. These tests hold
the legend to the enums member by member, and hold the builder to its refusal,
so a new status cannot ship as a bare token again.
"""

from __future__ import annotations

from enum import StrEnum

import pytest
from fastapi.testclient import TestClient

from local_first_agent_os.api import create_app
from local_first_agent_os.contracts import (
    TERMINAL_DISPATCH_INTENT_STATUSES,
    DispatchIntentStatus,
)
from local_first_agent_os.work_units.lifecycle import (
    TERMINAL_MILESTONE_STATUSES,
    TERMINAL_PHASE_STATUSES,
    TERMINAL_WORK_UNIT_STATUSES,
    MilestoneExecutionStatus,
    PhaseStatus,
    WorkUnitStatus,
)
from local_first_agent_os.work_units.status_legend import (
    STATUS_LEGEND,
    IncompleteStatusLegend,
    legend_entries,
)

_SECTIONS = {
    "work_unit": (WorkUnitStatus, TERMINAL_WORK_UNIT_STATUSES),
    "phase": (PhaseStatus, TERMINAL_PHASE_STATUSES),
    "milestone": (MilestoneExecutionStatus, TERMINAL_MILESTONE_STATUSES),
    "dispatch": (DispatchIntentStatus, TERMINAL_DISPATCH_INTENT_STATUSES),
}


@pytest.mark.parametrize("section", sorted(_SECTIONS))
def test_the_legend_accounts_for_every_member_the_enum_can_express(section: str) -> None:
    """Every member, in declaration order, exactly once.

    Declaration order matters because the cockpit renders the legend as a list,
    and an order that differs from the enum's would invent a second ordering for
    the same vocabulary.
    """

    enum, _ = _SECTIONS[section]
    entries = getattr(STATUS_LEGEND, section)

    assert [entry.status for entry in entries] == [member.value for member in enum]


@pytest.mark.parametrize("section", sorted(_SECTIONS))
def test_every_entry_says_more_than_its_token(section: str) -> None:
    """A meaning or action that is blank or just the token is the old bare pill."""

    for entry in getattr(STATUS_LEGEND, section):
        assert entry.meaning.strip(), f"{section}.{entry.status} has no meaning"
        assert entry.operator_action.strip(), f"{section}.{entry.status} has no operator action"
        assert entry.meaning.strip() != entry.status
        assert entry.operator_action.strip() != entry.status


@pytest.mark.parametrize("section", sorted(_SECTIONS))
def test_terminal_flags_restate_the_domain_not_the_author(section: str) -> None:
    enum, terminal = _SECTIONS[section]
    entries = getattr(STATUS_LEGEND, section)

    assert {entry.status for entry in entries if entry.terminal} == {
        member.value for member in terminal
    }


class _Toy(StrEnum):
    A = "A"
    B = "B"


def test_the_builder_refuses_a_mapping_missing_a_member() -> None:
    """The totality guarantee itself, verified to fail.

    ``STATUS_LEGEND`` being complete today proves nothing about the day an enum
    gains a member; this drives the builder with the incomplete mapping that day
    would produce and asserts the import would crash rather than serve a bare
    token.
    """

    with pytest.raises(IncompleteStatusLegend, match="missing=\\['B'\\]"):
        legend_entries(_Toy, {_Toy.A: ("a meaning", "an action")}, terminal=frozenset())


def test_the_builder_refuses_a_key_from_a_different_enum() -> None:
    """Four keyed-by-member mappings sit side by side; the copy-paste is real."""

    entries = {
        _Toy.A: ("a meaning", "an action"),
        _Toy.B: ("b meaning", "b action"),
        PhaseStatus.PENDING: ("stray", "stray"),
    }

    with pytest.raises(IncompleteStatusLegend, match="foreign=\\['PENDING'\\]"):
        legend_entries(_Toy, entries, terminal=frozenset())  # type: ignore[arg-type]


def test_the_route_serves_the_legend_without_a_selected_work_unit(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legend explains statuses the operator has not clicked into yet."""

    monkeypatch.setattr("local_first_agent_os.api.get_runtime", lambda: runtime)
    monkeypatch.setattr("local_first_agent_os.api.get_settings", lambda: runtime.settings)
    monkeypatch.setattr("local_first_agent_os.api.launch_dbos", lambda: None)

    with TestClient(create_app()) as client:
        response = client.get("/status-legend")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == "status_legend.v1"
    for section, (enum, _) in _SECTIONS.items():
        assert [entry["status"] for entry in payload[section]] == [member.value for member in enum]
        for entry in payload[section]:
            assert entry["meaning"]
            assert entry["operator_action"]
