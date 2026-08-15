# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A cast is a panel, not a pipeline, and its members are governable principals."""

from __future__ import annotations

import pytest

from local_first_agent_os.decomposition import DEFAULT_CAST, _cast_plan
from local_first_agent_os.pow_wow.cast import CastMember, build_cast_tasks
from local_first_agent_os.pow_wow.protocol import TaskPurpose
from local_first_agent_os.staffing import Tier

MEMBERS = (
    CastMember(name="advocate", stance="Argue for it.", tier=Tier.JUNIOR),
    CastMember(name="skeptic", stance="Argue against it.", tier=Tier.JUNIOR),
)


def test_members_do_not_block_each_other_so_the_executor_can_fan_them_out() -> None:
    """The whole point of a panel is that its members do not see each other first.

    A member blocked on another is a pipeline, and a pipeline's second stance is
    a reaction to the first rather than an independent read.
    """

    tasks = build_cast_tasks(prefix="t", goal="Ship the thing?", members=MEMBERS)
    members = [task for task in tasks if task.role != "synthesizer"]

    assert len(members) == 2
    assert all(task.blocked_by == () for task in members)


def test_the_synthesizer_blocks_on_every_member() -> None:
    tasks = build_cast_tasks(prefix="t", goal="Ship the thing?", members=MEMBERS)
    synthesis = tasks[-1]

    assert synthesis.role == "synthesizer"
    assert set(synthesis.blocked_by) == {"t_advocate", "t_skeptic"}
    assert synthesis.judgment is not None
    assert synthesis.judgment.tier is Tier.SENIOR


def test_the_member_role_is_the_name_a_policy_section_would_use() -> None:
    """`policy_principal` resolves a role to a POLICIES.md section before the seat.

    So the role string is the whole coupling between a stance and its privileges,
    and a member whose role drifted from its name would be governed by the seat
    instead of by its own section without anything reporting it.
    """

    tasks = build_cast_tasks(prefix="t", goal="Ship the thing?", members=MEMBERS)

    for task, member in zip(tasks[:-1], MEMBERS, strict=True):
        assert task.role == member.name
        assert task.judgment is not None
        assert task.judgment.name == member.name
        assert task.judgment.stance == member.stance


def test_every_cast_task_is_advisory_and_carries_the_cast_dispatch_kind() -> None:
    tasks = build_cast_tasks(prefix="t", goal="Ship the thing?", members=MEMBERS)

    assert all(task.purpose is TaskPurpose.ADVISORY for task in tasks)
    assert all(task.dispatch_kind == "cast" for task in tasks)


def test_a_cast_of_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least two stances"):
        build_cast_tasks(prefix="t", goal="?", members=MEMBERS[:1])


def test_duplicate_member_names_are_refused() -> None:
    """Two members sharing a name share a POLICIES principal and a task name."""

    duplicated = (*MEMBERS, CastMember(name="advocate", stance="Argue for it again."))
    with pytest.raises(ValueError, match="unique"):
        build_cast_tasks(prefix="t", goal="?", members=duplicated)


@pytest.mark.parametrize(
    ("name", "stance"),
    [
        ("", "Argue for it."),
        ("Advocate", "Argue for it."),
        (" advocate", "Argue for it."),
        ("advocate", "   "),
    ],
)
def test_a_member_that_could_not_resolve_to_a_principal_is_refused(name: str, stance: str) -> None:
    with pytest.raises(ValueError):
        CastMember(name=name, stance=stance)


def test_the_default_cast_does_not_seat_every_stance_on_one_model() -> None:
    """Same-model stances agree about their weights rather than about the question.

    This is the objection that parked the homogeneous junior swarm on 2026-08-05,
    and a default cast that ignored it would rebuild the thing that was rejected.
    """

    assert len(DEFAULT_CAST) >= 3
    assert len({member.name for member in DEFAULT_CAST}) == len(DEFAULT_CAST)


def test_the_cast_plan_is_reachable_from_a_cast_dispatch() -> None:
    """`_cast_plan` is what the `cast` branch of the planner selects."""

    tasks = _cast_plan("intent7", "Should we launch in March?")

    assert [task.task_name for task in tasks][-1] == "intent7_synthesis"
    assert set(tasks[-1].blocked_by) == {task.task_name for task in tasks[:-1]}
    assert "Should we launch in March?" in tasks[0].description
