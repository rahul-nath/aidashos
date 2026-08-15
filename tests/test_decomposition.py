# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import pytest

from local_first_agent_os.decomposition import (
    DecompositionError,
    PromptedDecompositionPlanner,
    RuleBasedDecompositionPlanner,
    parse_task_specs_from_planner_payload,
)
from local_first_agent_os.pow_wow import DispatchKind
from local_first_agent_os.staffing import Tier


class _Project:
    id = "target"
    kind = "repo"
    read_only = False


def _mini_gawd_payload() -> dict[str, object]:
    return {
        "project": "target",
        "status": "MVP",
        "time_budget": [
            {"phase": "scope", "hours": "0.25h", "deliverable": "bounded plan"},
            {"phase": "execute", "hours": "0.5h", "deliverable": "verified output"},
        ],
        "theory": "Durable task DAG over tiered agent execution.",
        "why": "Prevent drift while producing a bounded result.",
        "golden_flow": ["claim intent", "record plan", "run tasks"],
        "scope": {
            "in": ["bounded decomposition"],
            "non_goals": ["full GAWD", "auto-merge"],
        },
        "core_design": {
            "unit_of_work": "one decomposition plan",
            "lifecycle": ["planned", "completed|failed"],
            "data_model": ["ledger rows and artifacts"],
        },
        "failure_that_matters": "scope drift into irreversible action",
        "verification": ["task output is captured"],
        "decision_log": [
            {
                "decision_id": "D1",
                "decision": "keep mini-GAWD as artifact",
                "rationale": "guardrails without extra hop",
            }
        ],
        "deferred": ["full spec"],
    }


def test_rule_based_planner_emits_code_dag() -> None:
    plan = RuleBasedDecompositionPlanner().plan(
        intent_id="abcdef12-3456",
        tier=Tier.SENIOR,
        kind="code",
        prompt="implement the thing",
        target_project=_Project(),  # type: ignore[arg-type]
        intent={},
    )

    assert plan.schema_version == "decomposition_plan.v1"
    assert plan.mini_gawd.schema_version == "mini_gawd_doc.v1"
    assert plan.mini_gawd.project == "target"
    assert "No automatic merge" in plan.mini_gawd.non_goals[0]
    assert [task.judgment.tier for task in plan.tasks if task.judgment] == [
        Tier.SENIOR,
        Tier.STAFF,
        Tier.JUNIOR,
        Tier.SENIOR,
        Tier.STAFF,
    ]
    assert [task.dispatch_kind for task in plan.tasks] == [
        "advisory",
        "advisory",
        "advisory",
        "code",
        "code",
    ]
    assert plan.tasks[2].blocked_by == (plan.tasks[0].task_name,)
    assert set(plan.tasks[3].blocked_by) == {
        plan.tasks[0].task_name,
        plan.tasks[2].task_name,
    }
    assert set(plan.tasks[4].blocked_by) == {
        plan.tasks[1].task_name,
        plan.tasks[3].task_name,
    }
    assert plan.tasks[3].worktree_group == plan.tasks[4].worktree_group
    assert all("implement the thing" not in task.description for task in plan.tasks)
    assert "raw saga contract" in plan.tasks[0].description
    assert "saga goal above" in plan.tasks[3].description
    assert "blocked_by" in plan.planner_prompt


def test_planner_payload_parser_rejects_missing_dependency() -> None:
    with pytest.raises(DecompositionError, match="missing dependencies"):
        parse_task_specs_from_planner_payload(
            {
                "tasks": [
                    {
                        "task_name": "review",
                        "role": "reviewer",
                        "tier": "staff",
                        "description": "review",
                        "blocked_by": ["missing"],
                    }
                ]
            },
            default_dispatch_kind="advisory",
        )


def test_planner_payload_parser_accepts_valid_model_shape() -> None:
    tasks = parse_task_specs_from_planner_payload(
        {
            "tasks": [
                {
                    "task_name": "draft",
                    "role": "junior_context",
                    "tier": "junior",
                    "dispatch_kind": "advisory",
                    "description": "draft context",
                },
                {
                    "task_name": "verdict",
                    "role": "reviewer",
                    "tier": "staff",
                    "dispatch_kind": "advisory",
                    "description": "review draft",
                    "blocked_by": ["draft"],
                },
            ]
        },
        default_dispatch_kind="advisory",
    )

    assert len(tasks) == 2
    assert tasks[1].blocked_by == ("draft",)
    assert tasks[1].judgment and tasks[1].judgment.tier == Tier.STAFF


def test_prompted_planner_uses_injected_model_front_end() -> None:
    prompts: list[str] = []

    def fake_model(prompt: str) -> dict[str, object]:
        prompts.append(prompt)
        return {
            "rationale": "split into draft and verdict",
            "mini_gawd": _mini_gawd_payload(),
            "tasks": [
                {
                    "task_name": "draft",
                    "role": "junior_context",
                    "tier": "junior",
                    "description": "draft context",
                },
                {
                    "task_name": "verdict",
                    "role": "reviewer",
                    "tier": "staff",
                    "description": "review draft",
                    "blocked_by": ["draft"],
                },
            ],
        }

    plan = PromptedDecompositionPlanner(fake_model).plan(
        intent_id="intent-1",
        tier=Tier.STAFF,
        kind="advisory",
        prompt="decide what to do",
        target_project=_Project(),  # type: ignore[arg-type]
        intent={},
    )

    assert prompts and "Output JSON only" in prompts[0]
    assert "mini_gawd" in prompts[0]
    assert plan.rationale == "split into draft and verdict"
    assert plan.mini_gawd.theory == "Durable task DAG over tiered agent execution."
    assert plan.tasks[1].blocked_by == ("draft",)


def test_prompted_planner_requires_mini_gawd() -> None:
    planner = PromptedDecompositionPlanner(
        lambda _prompt: {
            "rationale": "missing scoped design brief",
            "tasks": [
                {
                    "task_name": "draft",
                    "role": "junior",
                    "tier": "junior",
                    "description": "draft",
                }
            ],
        }
    )

    with pytest.raises(DecompositionError, match="mini_gawd"):
        planner.plan(
            intent_id="intent-1",
            tier=Tier.JUNIOR,
            kind="advisory",
            prompt="draft",
            target_project=_Project(),  # type: ignore[arg-type]
            intent={},
        )


def test_dispatch_kind_type_exports_expected_values() -> None:
    kinds: tuple[DispatchKind, DispatchKind] = ("advisory", "code")
    assert kinds == ("advisory", "code")
