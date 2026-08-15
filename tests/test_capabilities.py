# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One name for a capability, and the drift that made four names necessary."""

from __future__ import annotations

from typing import Any

import pytest

from local_first_agent_os.capabilities import (
    Capability,
    UnknownCapability,
    gated_capabilities,
    parse_capability,
    violation_cleared_by,
)
from local_first_agent_os.policies import (
    _EXTERNAL_COMMS_TOOLS,
    _MERGE_TOOLS,
    _PURCHASE_TOOLS,
    PolicyViolation,
)
from local_first_agent_os.work_units.executors import EXECUTOR_REGISTRY

# The tools `ToolRegistry` actually dispatches. Written out rather than imported
# because constructing the registry needs a Repository; the guard below is that
# these two lists must not drift, which is the whole point of the enum.
REGISTERED_TOOLS = frozenset(
    {
        "apple_notes_fetch",
        "chrome_devtools",
        "workflowy_day_bullet_insert",
        "workflowy_fetch_nodes",
        "workflowy_insert_node",
    }
)


def test_every_registered_tool_is_a_capability() -> None:
    """A tool the registry can run must be nameable in a grant.

    If it is not, an operator cannot grant it and a policy cannot mention it,
    which is how `search_embeddings` ended up in a workspace allowlist while the
    registry had never heard of it.
    """

    missing = REGISTERED_TOOLS - {item.value for item in Capability}
    assert not missing, f"registered tools with no Capability member: {sorted(missing)}"


def test_every_executor_permission_is_a_capability() -> None:
    """The compiled plan's tool policy is spelled in the same enum."""

    for kind, declaration in EXECUTOR_REGISTRY.items():
        for permitted in declaration.permitted_tools:
            assert isinstance(permitted, Capability), f"{kind} permits a bare string: {permitted!r}"


def test_an_unknown_capability_names_the_known_ones() -> None:
    """The error carries the answer, because the caller mistyped a name."""

    with pytest.raises(UnknownCapability) as caught:
        parse_capability("write_repositry")
    assert "write_repositry" in str(caught.value)
    assert "write_repository" in str(caught.value)


def test_parsing_tolerates_surrounding_space_only() -> None:
    assert parse_capability("  run_command  ") is Capability.RUN_COMMAND
    with pytest.raises(UnknownCapability):
        parse_capability("Run_Command")


def test_the_plan_payload_stays_plain_strings() -> None:
    """The hash is over JSON, so the enum must not change the serialized bytes.

    Typing `permitted_tools` was meant to be a compile-time change only. If a
    payload started carrying enum reprs instead of their values, every existing
    plan hash would move and `from_payload` would reject rows it wrote itself.
    """

    declaration = EXECUTOR_REGISTRY[next(iter(EXECUTOR_REGISTRY))]
    payload: dict[str, Any] = declaration.to_payload()
    assert all(isinstance(item, str) for item in payload["permitted_tools"])
    assert all(not item.startswith("Capability.") for item in payload["permitted_tools"])


# Which capabilities an approval stands behind.
UNGATED = frozenset(
    {
        Capability.APPLE_NOTES_FETCH,
        Capability.ASK_OPERATOR,
        Capability.CHROME_DEVTOOLS,
        Capability.INVOKE_MODEL,
        Capability.READ_REPOSITORY,
        Capability.WORKFLOWY_FETCH_NODES,
        Capability.WRITE_ARTIFACT,
    }
)


def test_a_new_capability_has_to_declare_whether_an_approval_stands_behind_it() -> None:
    """The one list somebody must edit when they add a capability.

    A capability missing from `_CLEARS` is ungated, and ungated means the gate's
    revocation path can never refuse it. That is the safe default for a read and
    a silent hole for anything else, and the difference is invisible at the point
    where the new member gets added.

    So the ungated set is written out here instead of derived. Adding a
    capability fails this test until somebody says which of the two it is.
    """

    assert set(Capability) - gated_capabilities() == UNGATED


# What `check_capability` is actually able to refuse. Written out because the
# enum implies a far larger reach than the gate has, and that gap is the thing
# worth pinning.
GATE_REACHES = frozenset(
    {
        Capability.READ_REPOSITORY,
        Capability.WRITE_REPOSITORY,
        Capability.RUN_COMMAND,
        Capability.INVOKE_MODEL,
        Capability.WRITE_ARTIFACT,
        Capability.ASK_OPERATOR,
        Capability.PUBLISH_DEPLOYMENT,
    }
)


def test_the_gate_only_reaches_capabilities_an_executor_declaration_grants() -> None:
    """The honest scope of `check_capability`, and it is narrower than it looks.

    `check_capability` has exactly one production caller: `_authorize_spawn` in
    `pow_wow/executor.py`, which iterates `authority.capabilities &
    gated_capabilities()`. So a capability the gate can refuse must be one some
    `ExecutorDeclaration` grants. Nothing else in the enum is reachable from it.

    Two consequences a reader should not have to discover the hard way.

    A capability nothing grants is inert however it is gated. `merge_to_main`,
    `spend_money`, `access_credentials`, `destructive_file_operations`,
    `external_communications`, and `network_access` appear only in `_CLEARS` and
    `ACTION_CAPABILITIES`: no executor declaration names one and no
    `authority_for_purpose` branch returns one, so the intersection above never
    contains them and gating them refuses nothing today. They are gated anyway,
    because a plan *can* deny them and the day a role authority grants one is the
    day the gate needs to already know they are consequential.

    And the registry tools are governed somewhere else entirely.
    `chrome_devtools`, `apple_notes_fetch`, and the `workflowy_*` members are
    dispatched by `ToolRegistry`, which consults `PolicyStore` and never calls
    this gate. Their membership in `Capability` is what makes `_CLEARS` look like
    it governs them. It does not.
    """

    assert set(Capability) > GATE_REACHES

    registry_tools = {
        Capability.APPLE_NOTES_FETCH,
        Capability.CHROME_DEVTOOLS,
        Capability.WORKFLOWY_FETCH_NODES,
        Capability.WORKFLOWY_INSERT_NODE,
        Capability.WORKFLOWY_DAY_BULLET_INSERT,
    }
    assert not (registry_tools & GATE_REACHES), (
        "a registry tool became spawn-grantable; it is now reachable from the "
        "capability gate and its gating status stops being decorative"
    )


def test_no_capability_value_is_a_tool_name_the_policy_rules_match() -> None:
    """The residual leg of `check_capability` cannot refuse anything.

    `check_capability` ends by calling `SagaPolicyEngine.check_tool_call` with
    `capability.value` as the tool name. That function matches tool names against
    hardcoded sets - `send_email`, `git_merge`, `stripe_charge` - and no
    `Capability` value appears in any of them, so it returns allowed every time.

    This is asserted rather than fixed because the two are deliberately different
    vocabularies, and the failure mode of "fixing" it is worse than the dead
    branch: `_CLEARS` is many-to-one, and `granted_violations_for` returns
    approval *classes*, so the moment a capability value matched a rule, a grant
    of `workflowy_day_bullet_insert` would clear `NO_EXTERNAL_COMMS` for
    `access_credentials` and `publish_deployment` too.

    If this test ever fails, that widening has gone live. Read
    `granted_violations_for` before celebrating.
    """

    matched = {capability.value for capability in Capability} & (
        _EXTERNAL_COMMS_TOOLS | _MERGE_TOOLS | _PURCHASE_TOOLS
    )

    assert matched == set(), (
        f"{sorted(matched)} now matches a policy rule set; a grant of any capability "
        "sharing its approval class silently clears it for every other capability "
        "in that class"
    )


def test_every_approval_class_the_engine_knows_can_be_cleared() -> None:
    """A violation class no capability clears is an approval nobody can satisfy.

    `SagaPolicyEngine` refuses a tool call by naming one of these. If no
    capability maps to it, an operator granting every capability in the enum
    would still be refused, with nothing they could grant to get past it.
    """

    cleared = {violation_cleared_by(capability) for capability in gated_capabilities()}

    assert set(PolicyViolation) - cleared - {PolicyViolation.NO_MODEL_ESCALATION} == set()
