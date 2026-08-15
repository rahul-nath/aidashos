# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What a dispatch source says about a saga milestone, case by case.

The parser used to return `str | None`, which gave two different situations the
same answer: a source that never claimed a milestone, and a source that claimed
one and carried no identifier. Both came back `None` and both were skipped in
silence. These are the cases that distinction was hiding.

The fifth case is not the parser's: a source can carry a well-formed identifier
that resolves to no row. Only a lookup can tell, so that one is asserted against
the claim path rather than against the parser.
"""

from __future__ import annotations

import json

from local_first_agent_os.coordination import store
from local_first_agent_os.coordination.dispatch import (
    claim_next_dispatch_intent,
    submit_dispatch_intent,
)
from local_first_agent_os.coordination.milestones import (
    SAGA_MILESTONE_SOURCE_MARKER,
    ClaimedMilestone,
    MalformedMilestoneReference,
    NoMilestoneReference,
    parse_milestone_reference,
)
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.workflow.saga_support import (
    build_approved_gawd_milestone_dispatch_source,
)


def test_a_source_built_by_the_builder_round_trips() -> None:
    """The one writer and the one reader agree, which is the only contract here."""

    source = build_approved_gawd_milestone_dispatch_source("doc-1", "mile-1")

    assert parse_milestone_reference(source) == ClaimedMilestone(milestone_id="mile-1")


def test_no_source_at_all_claims_nothing() -> None:
    assert parse_milestone_reference(None) == NoMilestoneReference()
    assert parse_milestone_reference("") == NoMilestoneReference()


def test_a_source_without_the_marker_claims_nothing() -> None:
    assert parse_milestone_reference("approved_gawd:doc-1") == NoMilestoneReference()


def test_a_work_unit_source_claims_nothing() -> None:
    """The WorkUnit engine's intents must not reach the legacy milestone lane.

    This is the property that keeps two lifecycle authorities from writing the
    same milestone, and it has never been asserted anywhere. It holds because
    `milestone_execution` is not `milestone`, which is a thin thing to rest on -
    hence the test.
    """

    from local_first_agent_os.work_units.execution import DispatchBackedExecutorRuntime

    class _Milestone:
        stable_key = "m01"

    class _Context:
        work_unit_id = "wu-1"
        milestone = _Milestone()

    source = DispatchBackedExecutorRuntime().dispatch_source(_Context())  # type: ignore[arg-type]

    assert SAGA_MILESTONE_SOURCE_MARKER not in source
    assert parse_milestone_reference(source) == NoMilestoneReference()


def test_the_marker_with_nothing_after_it_is_malformed() -> None:
    """Distinct from "no claim", which is the whole point of the change."""

    source = f"approved_gawd:doc-1{SAGA_MILESTONE_SOURCE_MARKER}"

    assert parse_milestone_reference(source) == MalformedMilestoneReference(source=source)
    assert parse_milestone_reference(f"{source}   ") == MalformedMilestoneReference(
        source=f"{source}   "
    )


def test_the_builder_refuses_to_produce_a_malformed_source() -> None:
    """Better than reporting it later: the caller has the id in hand."""

    import pytest

    with pytest.raises(ValueError, match="needs a milestone id"):
        build_approved_gawd_milestone_dispatch_source("doc-1", "  ")


def test_a_dangling_milestone_reference_is_recorded_rather_than_ignored(tmp_path) -> None:
    """A claim that resolves to no row is not the same as no claim.

    Retention can remove a milestone out from under a live intent, so the claim
    still proceeds. What changed is that it stops being invisible: the guards
    were all `bool(milestone and ...)`, so a dangling reference satisfied every
    one of them and the IN_PROGRESS write then matched zero rows in silence.
    """

    store.set_root(str(tmp_path))
    saga_id = str(create_saga("dangling reference")["saga_id"])
    source = build_approved_gawd_milestone_dispatch_source("doc-1", "milestone-that-never-existed")
    submit_dispatch_intent("senior", "do the work", "code", None, source)

    result = claim_next_dispatch_intent("worker-1")

    assert result["intent"] is not None, "the claim proceeds; the milestone is merely gone"
    assert result["intent"]["source"] == source
    # The outbox mirror is off by default in tests, so the event log on disk is
    # where emit() always lands.
    recorded = [
        json.loads(line)["event_type"]
        for line in store.events_path().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "dispatch_milestone_reference_unresolved" in recorded
    assert saga_id
