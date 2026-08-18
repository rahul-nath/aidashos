# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The authoring vocabulary for a WorkUnit permission envelope.

The intake document speaks in operator actions such as ``code_worktree_write``
and ``dependency_install``.  The process supervisor speaks in capabilities such
as ``write_repository`` and ``run_command``.  Those are different levels of the
same decision, so the translation is total, typed, and owned here rather than
repeated by the parser, compiler, and Cockpit.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ..capabilities import Capability


class PermissionAction(StrEnum):
    """Every action the generated permission envelope may name."""

    READ_REPO_CONTEXT = "read_repo_context"
    WRITE_LEDGER_ARTIFACTS = "write_ledger_artifacts"
    RUN_LOCAL_MODEL_DELEGATES = "run_local_model_delegates"
    PREPARE_ISOLATED_WORKTREES = "prepare_isolated_worktrees"
    REQUEST_OPERATOR_DECISIONS = "request_operator_decisions"
    CODE_WORKTREE_WRITE = "code_worktree_write"
    TEST_COMMAND_EXECUTION = "test_command_execution"
    DEPENDENCY_INSTALL = "dependency_install"
    NETWORK_ACCESS = "network_access"
    DEPLOY = "deploy"
    EXTERNAL_COMMUNICATIONS = "external_communications"
    SPEND_MONEY = "spend_money"
    MERGE_TO_MAIN = "merge_to_main"
    PURCHASE_OR_SPEND = "purchase_or_spend"
    SECRET_OR_CREDENTIAL_ACCESS = "secret_or_credential_access"
    DESTRUCTIVE_FILE_OPERATIONS = "destructive_file_operations"


# One authored action may need several runtime capabilities.  An install, for
# example, is both a command and network egress.  Every enum member appears so a
# new action cannot compile until its execution meaning is decided.
ACTION_CAPABILITIES: Final = MappingProxyType(
    {
        PermissionAction.READ_REPO_CONTEXT: (Capability.READ_REPOSITORY,),
        PermissionAction.WRITE_LEDGER_ARTIFACTS: (Capability.WRITE_ARTIFACT,),
        PermissionAction.RUN_LOCAL_MODEL_DELEGATES: (Capability.INVOKE_MODEL,),
        PermissionAction.PREPARE_ISOLATED_WORKTREES: (),
        PermissionAction.REQUEST_OPERATOR_DECISIONS: (Capability.ASK_OPERATOR,),
        PermissionAction.CODE_WORKTREE_WRITE: (Capability.WRITE_REPOSITORY,),
        PermissionAction.TEST_COMMAND_EXECUTION: (Capability.RUN_COMMAND,),
        PermissionAction.DEPENDENCY_INSTALL: (
            Capability.RUN_COMMAND,
            Capability.NETWORK_ACCESS,
        ),
        PermissionAction.NETWORK_ACCESS: (Capability.NETWORK_ACCESS,),
        PermissionAction.DEPLOY: (Capability.PUBLISH_DEPLOYMENT,),
        PermissionAction.EXTERNAL_COMMUNICATIONS: (Capability.EXTERNAL_COMMUNICATIONS,),
        PermissionAction.SPEND_MONEY: (Capability.SPEND_MONEY,),
        PermissionAction.MERGE_TO_MAIN: (Capability.MERGE_TO_MAIN,),
        PermissionAction.PURCHASE_OR_SPEND: (Capability.SPEND_MONEY,),
        PermissionAction.SECRET_OR_CREDENTIAL_ACCESS: (Capability.ACCESS_CREDENTIALS,),
        PermissionAction.DESTRUCTIVE_FILE_OPERATIONS: (Capability.DESTRUCTIVE_FILE_OPERATIONS,),
    }
)


def capabilities_for_actions(
    actions: tuple[PermissionAction, ...],
) -> tuple[Capability, ...]:
    """Return the stable union of the capabilities these actions require."""

    return tuple(
        sorted(
            {capability for action in actions for capability in ACTION_CAPABILITIES[action]},
            key=lambda item: item.value,
        )
    )


# --------------------------------------------------------------------------- #
# The baseline envelope
# --------------------------------------------------------------------------- #
#
# What a document gets when it declares no Permission Envelope of its own.
#
# It lives here, once, because two readers need it and they must not disagree:
# `new_project_intake.permission_envelope_for_draft` writes it into the
# finalized draft an operator approves, and `work_units.compiler` compiles it
# into the capability ceiling every milestone runs under. A second copy in
# either place would let an operator approve one envelope and an agent run
# under another.
#
# What no build needs a grant for. Reading, recording, asking, and preparing a
# worktree change nothing outside the run.
BASELINE_AUTONOMOUS_ACTIONS: Final[tuple[PermissionAction, ...]] = (
    PermissionAction.READ_REPO_CONTEXT,
    PermissionAction.WRITE_LEDGER_ARTIFACTS,
    PermissionAction.RUN_LOCAL_MODEL_DELEGATES,
    PermissionAction.PREPARE_ISOLATED_WORKTREES,
    PermissionAction.REQUEST_OPERATOR_DECISIONS,
)

# Writing an isolated worktree and running the declared tests: what "build" means
# here, and the two capabilities without which no implement or verify milestone
# can act at all.
#
# The two intake paths read this tuple differently, and the difference is
# deliberate rather than drift.
#
# `new_project_intake` puts these in *requested*, because that path ends at a
# real operator review - `/start /approved-gawd` on a finalized draft - and
# these two are the substance of what is being approved there.
#
# `compiler` grants them *autonomously* to a document that declared no envelope
# at all. That is not a relaxation: an undeclared document used to compile with
# no ceiling whatsoever and no start approval, so the baseline replaces
# unlimited-and-ungated with these-and-nothing-else. Making them requested
# instead would put a mandatory approval in front of every document ever
# compiled, and a gate that appears on everything is one an operator learns to
# clear without reading - the same reflex this whole change exists to stop.
#
# A document that says nothing is not asking for anything, so there is nothing
# to approve. A document that writes "Requested permissions:" is asking, and
# `requires_start_approval` gates it.
#
# Reasons travel with the actions rather than being written at each call site,
# because the reason is what the operator reads when deciding whether to grant.
BASELINE_BUILD_ACTIONS: Final[tuple[tuple[PermissionAction, str], ...]] = (
    (
        PermissionAction.CODE_WORKTREE_WRITE,
        "Implementation agents need isolated write access to complete build tasks.",
    ),
    (
        PermissionAction.TEST_COMMAND_EXECUTION,
        "Agents need to run verification commands from the approved GAWD doc.",
    ),
)

# Never available to a document that did not name them. Each one is either
# irreversible or spends something outside the run.
BASELINE_DENIED_ACTIONS: Final[tuple[PermissionAction, ...]] = (
    PermissionAction.MERGE_TO_MAIN,
    PermissionAction.DEPLOY,
    PermissionAction.PURCHASE_OR_SPEND,
    PermissionAction.EXTERNAL_COMMUNICATIONS,
    PermissionAction.SECRET_OR_CREDENTIAL_ACCESS,
    PermissionAction.DESTRUCTIVE_FILE_OPERATIONS,
)


__all__ = [
    "ACTION_CAPABILITIES",
    "BASELINE_AUTONOMOUS_ACTIONS",
    "BASELINE_BUILD_ACTIONS",
    "BASELINE_DENIED_ACTIONS",
    "PermissionAction",
    "capabilities_for_actions",
]
