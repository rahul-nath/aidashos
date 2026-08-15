# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The two read commands an operator runs when a WorkUnit went wrong.

Both were broken on 2026-08-09, in ways that only appear at the CLI and MCP
edge, and both were found the same way: by an operator following
``docs/cockpit_e2e_runbook.md`` to find out why a real run blocked.

The runbook's troubleshooting section says "read the artifact bodies, not just
their types", and `list_work_unit_artifacts` could not print an artifact at all.
`ArtifactType` is a sum of dataclasses rather than a string, `service` put the
object straight into its payload, and `json.dumps` raised `TypeError: Object of
type RequirableArtifact is not JSON serializable`. The cockpit never saw it
because `projection.py` builds its own view, so the break was invisible to every
surface except the one the runbook recommends.

The second is worse for being quiet. An unknown work unit id returned
``{"ok": true, "artifacts": []}``, which reads as "this run produced no
evidence" and is really "you asked about a WorkUnit that does not exist". An
operator pasted a truncated id, got the empty answer, and took it for a finding
about a blocked run. `get_work_unit` refused the same input with `unknown work
unit`, so two commands at one layer disagreed about what a bad id means.

These are edge tests on purpose. The domain objects were correct throughout and
the existing suite exercised them; what nothing exercised was the JSON a person
actually reads.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from work_unit_support import install_simulated_engine, run_acceptance_work_unit

from local_first_agent_os.work_units import commands


@pytest.fixture
def work_unit_id() -> str:
    """A completed WorkUnit, which is the only kind that has artifacts to print.

    The simulated engine is what makes the milestones execute in this process;
    without it a dispatch-backed milestone parks waiting for a runtime that no
    test has.
    """

    install_simulated_engine()
    return run_acceptance_work_unit()


def test_artifacts_survive_the_json_encoder(work_unit_id: str) -> None:
    """The command must print, which means its payload must serialize.

    `json.dumps` here rather than a shape assertion, because the defect was not
    a wrong value: it was a payload that raised on the way to the terminal, and
    only a test that encodes it sees that.
    """

    payload = commands.list_work_unit_artifacts(work_unit_id)

    assert payload["ok"] is True
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    assert work_unit_id in encoded


def test_every_artifact_type_reads_as_its_name(work_unit_id: str) -> None:
    """`implementation_plan`, not `RequirableArtifact(kind=...)`.

    The runbook tells an operator to compare artifact types and read their
    bodies, so the type has to arrive as the word the document uses.
    """

    artifacts: list[dict[str, Any]] = commands.list_work_unit_artifacts(work_unit_id)["artifacts"]

    assert artifacts, "the acceptance document's WorkUnit produces evidence"
    for artifact in artifacts:
        assert isinstance(artifact["artifact_type"], str)
        assert artifact["artifact_type"] == artifact["artifact_type"].lower()


@pytest.mark.parametrize(
    "command",
    [commands.list_work_unit_artifacts, commands.list_work_unit_events],
    ids=["artifacts", "events"],
)
def test_an_unknown_work_unit_is_refused_rather_than_answered_emptily(
    command: Any,
) -> None:
    """An empty answer and a wrong question must not look the same.

    A truncated id is the ordinary way to ask a wrong question: ids are 32 hex
    characters and get copied out of messages and terminals. Answering `ok` with
    an empty list tells the operator something about their run, and it is false.
    """

    payload = command("an_id_no_work_unit_has")

    assert payload["ok"] is False
    assert "an_id_no_work_unit_has" in payload["message"]


def test_an_empty_answer_for_a_real_work_unit_is_still_legitimate(
    work_unit_id: str,
) -> None:
    """The check is on the WorkUnit, not on the row count.

    A real WorkUnit that has produced nothing yet must answer `ok` with an empty
    list, or the refusal above would turn every young run into an error.
    """

    events = commands.list_work_unit_events(work_unit_id, after_sequence=10**9)

    assert events["ok"] is True
    assert events["events"] == []
