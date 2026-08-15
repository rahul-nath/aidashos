# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Saga status is a projection of milestones, and intake dedupes by content.

Two bugs are pinned here, both from the 2026-07-25 handoff:

* Priority 1, every saga sat at its creation-time ``IDEA_INTAKE``/``PLANNING``
  while its milestones ran to completion, because the milestone path never
  wrote ``sagas.status`` and an imperative stage setter was reachable from one
  unrelated call site. Pest read ``PLANNING`` with five of six milestones done.
  That setter has since been removed; this projection is the only writer.
* Priority 3, five sagas shared the goal prefix "New project intake: Two live
  prospects exist" because nothing deduped a repeated ingest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from local_first_agent_os.contracts import MilestoneStatus, SagaStage, SagaStatus
from local_first_agent_os.coordination import store
from local_first_agent_os.coordination.contracts import CreateSaga
from local_first_agent_os.coordination.milestones import (
    complete_saga_milestone,
    create_saga_milestone,
    derive_saga_lifecycle,
    fail_saga_milestone,
    retry_saga_milestone,
    start_saga_milestone,
)
from local_first_agent_os.coordination.projects import (
    create_saga,
    saga_content_digest,
)
from local_first_agent_os.coordination.store import connect, set_root


@pytest.fixture(autouse=True)
def _disposable_ledger(tmp_path: Path):
    root = tmp_path / "coordination"
    root.mkdir(parents=True, exist_ok=True)
    set_root(str(root))
    store._SCHEMA_READY.clear()
    yield root
    set_root(None)
    store._SCHEMA_READY.clear()


def _saga_row(saga_id: str) -> dict[str, object]:
    with connect() as c:
        return dict(
            c.execute(
                "SELECT status, current_stage FROM sagas WHERE saga_id = ?", (saga_id,)
            ).fetchone()
        )


def _milestone(status: str, sequence: int = 1, approval_required: int = 0) -> dict[str, object]:
    return {"status": status, "sequence": sequence, "approval_required": approval_required}


# --- The derivation, in isolation --------------------------------------------


def test_a_saga_with_no_milestones_keeps_its_creation_time_value() -> None:
    """Nothing has happened to it, and saying otherwise would be a new lie."""

    assert derive_saga_lifecycle([]) is None


@pytest.mark.parametrize(
    ("milestones", "expected"),
    [
        pytest.param(
            [_milestone("PENDING", 1), _milestone("PENDING", 2)],
            ("PLANNING", "REQUIREMENT_DECOMPOSITION"),
            id="derived_but_not_started",
        ),
        pytest.param(
            [_milestone("COMPLETED", 1), _milestone("IN_PROGRESS", 2)],
            ("ACTIVE", "IMPLEMENTATION"),
            id="work_in_flight",
        ),
        pytest.param(
            [_milestone("COMPLETED", 1), _milestone("COMPLETED", 2)],
            ("COMPLETED", "USER_APPROVAL"),
            id="all_done",
        ),
        pytest.param(
            [_milestone("COMPLETED", 1), _milestone("PENDING", 2)],
            ("ACTIVE", "IMPLEMENTATION"),
            id="between_milestones",
        ),
        pytest.param(
            [_milestone("FAILED", 1), _milestone("PENDING", 2)],
            ("ACTIVE", "IMPLEMENTATION"),
            id="failed_is_still_active_because_retry_exists",
        ),
        pytest.param(
            [_milestone("COMPLETED", 1), _milestone("PENDING", 2, approval_required=1)],
            ("AWAITING_APPROVAL", "USER_APPROVAL"),
            id="next_milestone_is_operator_gated",
        ),
    ],
)
def test_lifecycle_is_derived_from_milestone_facts(
    milestones: list[dict[str, object]], expected: tuple[str, str]
) -> None:
    assert derive_saga_lifecycle(milestones) == expected


def test_a_running_milestone_outranks_a_later_approval_gate() -> None:
    """AWAITING_APPROVAL must mean "waiting on you", not "waiting on anything"."""

    assert derive_saga_lifecycle(
        [_milestone("IN_PROGRESS", 1), _milestone("PENDING", 2, approval_required=1)]
    ) == ("ACTIVE", "IMPLEMENTATION")


# --- Through the real ledger --------------------------------------------------


def test_the_pest_shape_no_longer_reports_planning() -> None:
    """The exact reported bug: five of six done, saga still at IDEA_INTAKE."""

    saga_id = create_saga("ship the pest factory")["saga_id"]
    milestone_ids = [
        create_saga_milestone(saga_id, f"m{index}", index)["milestone"]["milestone_id"]
        for index in range(1, 7)
    ]
    for milestone_id in milestone_ids[:5]:
        start_saga_milestone(milestone_id)
        complete_saga_milestone(milestone_id)

    assert _saga_row(saga_id) == {"status": "ACTIVE", "current_stage": "IMPLEMENTATION"}


def test_completing_every_milestone_completes_the_saga() -> None:
    saga_id = create_saga("small project")["saga_id"]
    first = create_saga_milestone(saga_id, "only", 1)["milestone"]["milestone_id"]

    assert _saga_row(saga_id)["status"] == "PLANNING"
    start_saga_milestone(first)
    assert _saga_row(saga_id) == {"status": "ACTIVE", "current_stage": "IMPLEMENTATION"}
    complete_saga_milestone(first)
    assert _saga_row(saga_id) == {"status": "COMPLETED", "current_stage": "USER_APPROVAL"}


def test_failing_and_retrying_moves_the_saga_back_and_forth() -> None:
    """A projection that only advances would be a second way to lie."""

    saga_id = create_saga("flaky project")["saga_id"]
    first = create_saga_milestone(saga_id, "one", 1)["milestone"]["milestone_id"]
    create_saga_milestone(saga_id, "two", 2)

    start_saga_milestone(first)
    fail_saga_milestone(first, "toolchain missing")
    assert _saga_row(saga_id) == {"status": "ACTIVE", "current_stage": "IMPLEMENTATION"}

    retry_saga_milestone(first, "node version pinned")
    assert _saga_row(saga_id) == {
        "status": "PLANNING",
        "current_stage": "REQUIREMENT_DECOMPOSITION",
    }


def test_creating_milestones_moves_a_saga_off_idea_intake() -> None:
    saga_id = create_saga("newly decomposed")["saga_id"]
    assert _saga_row(saga_id)["current_stage"] == "IDEA_INTAKE"

    create_saga_milestone(saga_id, "first", 1)
    assert _saga_row(saga_id)["current_stage"] == "REQUIREMENT_DECOMPOSITION"


# --- Intake dedupe ------------------------------------------------------------


def test_the_same_draft_replays_onto_one_saga() -> None:
    draft = "# THE GAWD DOC\n\nTwo live prospects exist.\n"
    digest = saga_content_digest(draft)

    first = create_saga("New project intake: Two live prospects exist", content_digest=digest)
    second = create_saga("New project intake: Two live prospects exist", content_digest=digest)

    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["saga_id"] == first["saga_id"]
    with connect() as c:
        assert len(c.execute("SELECT saga_id FROM sagas").fetchall()) == 1


def test_a_different_draft_is_a_different_saga() -> None:
    first = create_saga("a", content_digest=saga_content_digest("draft one"))
    second = create_saga("b", content_digest=saga_content_digest("draft two"))
    assert first["saga_id"] != second["saga_id"]


def test_the_same_goal_with_different_content_is_not_deduped() -> None:
    """Goal prefixes collide; content does not. That is why the key is content."""

    goal = "New project intake: Many individual pest control businesses"
    first = create_saga(goal, content_digest=saga_content_digest("draft A"))
    second = create_saga(goal, content_digest=saga_content_digest("draft B"))
    assert first["saga_id"] != second["saga_id"]


def test_callers_without_a_digest_keep_the_old_behavior() -> None:
    first = create_saga("untracked origin")
    second = create_saga("untracked origin")
    assert first["saga_id"] != second["saga_id"]
    assert first["replayed"] is False


def test_the_unique_index_backs_the_dedupe_up() -> None:
    """The check is a fast path; the index is what makes duplication impossible."""

    digest = saga_content_digest("one draft")
    create_saga("first", content_digest=digest)
    with connect() as c:
        indexes = [
            dict(row)
            for row in c.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname = 'idx_sagas_content_digest'"
            ).fetchall()
        ]
    assert indexes, "expected a unique index on sagas(content_digest)"


def test_the_digest_survives_the_cross_process_command_boundary() -> None:
    """Dedupe is in-process here but cross-process in the running system.

    `pi` reaches create_saga through argv, so a digest that never reaches the
    argv is a digest that never dedupes anything in production while every
    in-process test still passes. The flag is the seam, so the seam is pinned.
    """

    digest = saga_content_digest("one draft")

    argv = CreateSaga(goal="g", content_digest=digest).to_argv()

    assert "--content-digest" in argv
    assert argv[argv.index("--content-digest") + 1] == digest


def test_a_saga_without_a_digest_sends_no_flag() -> None:
    """Callers that do not dedupe must not start emitting an empty flag."""

    argv = CreateSaga(goal="g").to_argv()

    assert "--content-digest" not in argv


def _rows(*statuses: str, approval: int = 0) -> list[dict[str, Any]]:
    return [
        {"status": status, "sequence": index, "approval_required": approval}
        for index, status in enumerate(statuses, start=1)
    ]


@pytest.mark.parametrize(
    "statuses",
    [("BLOCKED",), ("BLOCKED", "PENDING"), ("CANCELED",), ("CANCELED", "PENDING")],
)
def test_a_halted_milestone_never_reads_as_planning(statuses: tuple[str, ...]) -> None:
    """BLOCKED and CANCELED were writable statuses no projection rule mentioned.

    Both fell through to the final branch, so a saga holding one reported
    PLANNING, which is the same lie this module was written to stop: "nothing
    has happened yet" about a saga something had already happened to.
    """

    derived = derive_saga_lifecycle(_rows(*statuses))

    assert derived is not None
    assert derived[0] is SagaStatus.ACTIVE
    assert derived[1] is SagaStage.IMPLEMENTATION


def test_every_milestone_status_is_classified() -> None:
    """The projection must have an answer for each member, not most of them.

    `_progress_of` is exhaustive under the type checker; this is the runtime
    half, so a member added without touching the classifier fails here too.
    """

    for status in MilestoneStatus:
        derived = derive_saga_lifecycle(_rows(status.value))
        assert derived is not None, f"{status.value} produced no saga lifecycle"
        assert isinstance(derived[0], SagaStatus)
        assert isinstance(derived[1], SagaStage)


def test_the_projection_returns_typed_members_not_bare_strings() -> None:
    """These two columns feed SagaStatus/SagaStage consumers, so type them."""

    derived = derive_saga_lifecycle(_rows("COMPLETED"))

    assert derived is not None
    assert derived == (SagaStatus.COMPLETED, SagaStage.USER_APPROVAL)
    assert isinstance(derived[0], SagaStatus)


def test_the_status_set_is_derived_from_the_enum() -> None:
    """One source of truth: the set used for validation is the enum itself."""

    from local_first_agent_os.coordination.milestones import _MILESTONE_STATUSES

    assert {status.value for status in MilestoneStatus} == _MILESTONE_STATUSES
