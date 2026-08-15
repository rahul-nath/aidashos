# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The one name for a thing an agent may do.

Four vocabularies described the same idea and shared one word between any two of
them: the workspace policy's ``allowed_tools``, the executor registry's
``permitted_tools``, ``allowed_tools_for_role`` (now deleted, it had no callers),
and the free-text ``tool_permission_requests.tool_name``. A grant written in one
could never be checked against a policy written in another, so the durable grant
ledger and the policy engine could not speak even once someone wired them.

This is that missing type. A capability that is not a member cannot be declared
by an executor, requested by an agent, or granted by an operator, which turns
"nobody registered this tool" into a crash instead of a silent bypass.

Two granularities live here on purpose. Some members are tools the
``ToolRegistry`` actually dispatches; others are abstract permissions an external
agent is given (``write_repository`` is not a registered tool, it is a statement
about what the agent may do inside its worktree). Both are things a grant needs
to be able to name, and splitting them would just recreate the problem this
module exists to solve.
"""

from __future__ import annotations

from enum import StrEnum

from .policies import PolicyViolation


class Capability(StrEnum):
    """Everything an agent can be permitted to do, named once."""

    # Dispatched by `ToolRegistry`. These names must match its keys exactly:
    # the registry is the thing that runs them, so it owns their spelling.
    APPLE_NOTES_FETCH = "apple_notes_fetch"
    CHROME_DEVTOOLS = "chrome_devtools"
    WORKFLOWY_DAY_BULLET_INSERT = "workflowy_day_bullet_insert"
    WORKFLOWY_FETCH_NODES = "workflowy_fetch_nodes"
    WORKFLOWY_INSERT_NODE = "workflowy_insert_node"

    # Granted to an external agent by an executor declaration. Not registry
    # entries: nothing dispatches `run_command`, it describes what the agent's
    # own harness is allowed to do on its behalf.
    ASK_OPERATOR = "ask_operator"
    ACCESS_CREDENTIALS = "access_credentials"
    DESTRUCTIVE_FILE_OPERATIONS = "destructive_file_operations"
    EXTERNAL_COMMUNICATIONS = "external_communications"
    INVOKE_MODEL = "invoke_model"
    MERGE_TO_MAIN = "merge_to_main"
    NETWORK_ACCESS = "network_access"
    PUBLISH_DEPLOYMENT = "publish_deployment"
    READ_REPOSITORY = "read_repository"
    RUN_COMMAND = "run_command"
    SPEND_MONEY = "spend_money"
    WRITE_ARTIFACT = "write_artifact"
    WRITE_REPOSITORY = "write_repository"


class UnknownCapability(ValueError):
    """A name that no capability answers to.

    Raised rather than returned because it is a programmer or configuration
    error, not a runtime condition: a grant for a capability that does not exist
    can never be satisfied, and failing at the moment it is written is the only
    point where the mistake is still cheap.
    """

    def __init__(self, name: str) -> None:
        known = ", ".join(sorted(item.value for item in Capability))
        super().__init__(f"unknown capability {name!r}; known capabilities: {known}")
        self.name = name


def parse_capability(name: str) -> Capability:
    """Read a capability name, or say precisely which names exist."""

    try:
        return Capability(name.strip())
    except ValueError as exc:
        raise UnknownCapability(name) from exc


# Which approval a grant of this capability satisfies.
#
# `SagaPolicyEngine.check_tool_call` takes `approved_actions`, a set of
# `PolicyViolation` values, and has had no producer since it was written: the
# grant ledger records a capability, the engine asks about a violation class, and
# nothing translated between them. This is that translation, and it is a mapping
# rather than a column on the ledger so that changing which approval a capability
# needs does not require rewriting rows an operator already granted.
#
# A capability absent from this mapping needs no approval, which is the honest
# default: `read_repository` is not a gated action, and inventing a violation
# class for it would make every read wait on a human.
#
# `PolicyViolation` rather than its string values, because the two lists have to
# agree and a typo between them would be silent: an unmatched class name reads as
# "this capability clears nothing", which is a refusal the engine can never make.
_CLEARS: dict[Capability, PolicyViolation] = {
    Capability.WRITE_REPOSITORY: PolicyViolation.NO_FILE_EDIT,
    Capability.RUN_COMMAND: PolicyViolation.NO_FILE_EDIT,
    Capability.DESTRUCTIVE_FILE_OPERATIONS: PolicyViolation.NO_FILE_EDIT,
    Capability.PUBLISH_DEPLOYMENT: PolicyViolation.NO_EXTERNAL_COMMS,
    Capability.WORKFLOWY_INSERT_NODE: PolicyViolation.NO_EXTERNAL_COMMS,
    Capability.WORKFLOWY_DAY_BULLET_INSERT: PolicyViolation.NO_EXTERNAL_COMMS,
    Capability.EXTERNAL_COMMUNICATIONS: PolicyViolation.NO_EXTERNAL_COMMS,
    Capability.NETWORK_ACCESS: PolicyViolation.NO_EXTERNAL_COMMS,
    Capability.ACCESS_CREDENTIALS: PolicyViolation.NO_EXTERNAL_COMMS,
    Capability.MERGE_TO_MAIN: PolicyViolation.NO_CODE_MERGE,
    Capability.SPEND_MONEY: PolicyViolation.NO_PURCHASE,
}


def violation_cleared_by(capability: Capability) -> PolicyViolation | None:
    """The approval class a grant of this capability satisfies, if any."""

    return _CLEARS.get(capability)


def gated_capabilities() -> frozenset[Capability]:
    """Capabilities that need an approval before they may run."""

    return frozenset(_CLEARS)


__all__ = [
    "Capability",
    "UnknownCapability",
    "gated_capabilities",
    "parse_capability",
    "violation_cleared_by",
]
