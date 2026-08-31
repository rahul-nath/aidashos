# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Two dispatches of the same tier must agree from the first byte.

Prompt caching is keyed on a prefix, so a shared block pays twice unless every
byte before it is also shared. `build_agent_task_prompt` used to open with the
role, the saga goal, and the target project, all of which differ on every
dispatch, so the doctrine contract sitting underneath them could never be
reused. The prompts diverged at byte zero and the largest stable block in the
system was bought fresh every time.

These assert the property rather than the ordering. A future edit is free to
rearrange the stable blocks among themselves; it is not free to put a varying
one in front of them.
"""

from __future__ import annotations

from pathlib import Path

from local_first_agent_os.coordination import DispatchKind
from local_first_agent_os.decomposition import RuleBasedDecompositionPlanner
from local_first_agent_os.pow_wow import PowWowExecutionContext
from local_first_agent_os.pow_wow.prompts import build_agent_task_prompt
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.vocabulary import DispatchTier


def _target(path: Path, project_id: str = "target") -> LinkedProject:
    path.mkdir(parents=True, exist_ok=True)
    return LinkedProject(
        id=project_id,
        kind="repo",
        path=path,
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="test target",
        verification_commands=[],
    )


def _plan(target: LinkedProject, prompt: str):
    return RuleBasedDecompositionPlanner().plan(
        intent_id="prefix-sharing",
        tier=DispatchTier.SENIOR,
        kind=DispatchKind.CODE,
        prompt=prompt,
        target_project=target,
        intent={},
    )


def _context(target: LinkedProject, goal: str, task_names: tuple[str, ...]):
    return PowWowExecutionContext(
        saga_id="saga-prefix",
        goal=goal,
        directive="/saga test",
        target_project_id=target.id,
        target_project_path=str(target.expanded_path),
        target_project_kind=target.kind,
        target_project_status=target.status,
        target_project_read_only=target.read_only,
        task_ids_by_name={name: f"task-{index}" for index, name in enumerate(task_names)},
    )


def _shared_prefix(left: str, right: str) -> str:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return left[:index]


def test_two_unrelated_dispatches_share_the_doctrine_prefix(tmp_path: Path) -> None:
    """The whole point. Different goal, different project, same stable opening.

    If this fails, every senior dispatch is paying for the doctrine contract
    again, and the failure is invisible: the prompts are still correct, just
    more expensive than they need to be.
    """

    first_target = _target(tmp_path / "one", "alpha")
    second_target = _target(tmp_path / "two", "beta")
    first_plan = _plan(first_target, "implement the raw contract")
    second_plan = _plan(second_target, "a completely different piece of work")

    first = build_agent_task_prompt(
        first_plan.tasks[0],
        _context(first_target, "goal one", tuple(t.task_name for t in first_plan.tasks)),
    )
    second = build_agent_task_prompt(
        second_plan.tasks[0],
        _context(
            second_target,
            "an entirely different goal",
            tuple(t.task_name for t in second_plan.tasks),
        ),
    )

    prefix = _shared_prefix(first, second)

    assert prefix.startswith("Startup:")
    assert "engineering_doctrine" in prefix or "Engineering" in prefix or len(prefix) > 2000, (
        "the shared prefix should reach into the doctrine contract, "
        f"but stopped after {len(prefix)} characters"
    )


def test_nothing_that_varies_appears_before_the_stable_blocks(tmp_path: Path) -> None:
    """Stated as the rule rather than as an ordering, so a rearrangement is free
    and a regression is not."""

    target = _target(tmp_path / "target", "gamma")
    plan = _plan(target, "implement the raw contract")
    goal = "a goal string that appears nowhere else"

    prompt = build_agent_task_prompt(
        plan.tasks[0],
        _context(target, goal, tuple(t.task_name for t in plan.tasks)),
    )

    for varying in (goal, target.id, str(target.expanded_path)):
        assert varying in prompt, "the varying value must still be present"
        assert prompt.index("Startup:") < prompt.index(varying), (
            f"{varying!r} appears before the stable opening, which truncates the "
            "cacheable prefix to nothing"
        )


def test_the_task_and_its_constraints_stay_last(tmp_path: Path) -> None:
    """Recency is what an instruction needs, and no caching gain is worth it."""

    target = _target(tmp_path / "target", "delta")
    plan = _plan(target, "implement the raw contract")

    prompt = build_agent_task_prompt(
        plan.tasks[0],
        _context(target, "goal", tuple(t.task_name for t in plan.tasks)),
    )

    assert prompt.index("Startup:") < prompt.index("Task:")
    assert prompt.rstrip().endswith(("the task is complete.", "verdict is complete."))


def test_the_doctrine_is_still_actually_there(tmp_path: Path) -> None:
    """Reordering must not drop a block. Cheap, and the failure would be silent."""

    target = _target(tmp_path / "target", "epsilon")
    plan = _plan(target, "implement the raw contract")

    prompt = build_agent_task_prompt(
        plan.tasks[0],
        _context(target, "goal", tuple(t.task_name for t in plan.tasks)),
    )

    assert "Context/token discipline:" in prompt
