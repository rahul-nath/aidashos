# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from .constants import LOCAL_AGENT_STATE_DIR_NAME
from .contracts import WorkspaceId, WorkspacePolicy, enum_values_by_name

if TYPE_CHECKING:
    # Annotation-only, and deferred to keep this module importable from
    # `settings`. `PolicyViolation` lives here and `capabilities` reads it, so a
    # runtime import of `Settings` would close the loop
    # settings -> access_posture -> capabilities -> policies -> settings.
    from .settings import Settings

WORKSPACE_IDS = enum_values_by_name(WorkspaceId)
HOME_PATH = Path.home()

DEFAULT_WORKSPACE_POLICIES: dict[str, WorkspacePolicy] = {
    WORKSPACE_IDS["GENERAL"]: WorkspacePolicy(
        workspace_id=WORKSPACE_IDS["GENERAL"],
        root_path=HOME_PATH,
        allowed_tools=["workflowy_fetch_nodes", "workflowy_insert_node"],
    ),
    WORKSPACE_IDS["WHITEBOARD_OCR"]: WorkspacePolicy(
        workspace_id=WORKSPACE_IDS["WHITEBOARD_OCR"],
        root_path=HOME_PATH / "AgentIngress" / "whiteboards",
        allowed_tools=[
            "workflowy_fetch_nodes",
            "workflowy_insert_node",
            "workflowy_day_bullet_insert",
        ],
    ),
    WORKSPACE_IDS["PAPER_NOTES"]: WorkspacePolicy(
        workspace_id=WORKSPACE_IDS["PAPER_NOTES"],
        root_path=HOME_PATH / "AgentIngress" / WORKSPACE_IDS["PAPER_NOTES"],
        allowed_tools=[
            "workflowy_fetch_nodes",
            "workflowy_insert_node",
            "workflowy_day_bullet_insert",
        ],
    ),
    WORKSPACE_IDS["APPLE_NOTES"]: WorkspacePolicy(
        workspace_id=WORKSPACE_IDS["APPLE_NOTES"],
        root_path=HOME_PATH,
        allowed_tools=["apple_notes_fetch", "workflowy_fetch_nodes", "workflowy_insert_node"],
    ),
    WORKSPACE_IDS["WORKFLOWY"]: WorkspacePolicy(
        workspace_id=WORKSPACE_IDS["WORKFLOWY"],
        root_path=HOME_PATH,
        allowed_tools=[
            "workflowy_fetch_nodes",
            "workflowy_insert_node",
            "workflowy_day_bullet_insert",
        ],
        write_enabled=False,
    ),
    WORKSPACE_IDS["CHROME"]: WorkspacePolicy(
        workspace_id=WORKSPACE_IDS["CHROME"],
        root_path=HOME_PATH,
        allowed_tools=["chrome_devtools"],
        forbidden_tools=["bash", "raw_http"],
        write_enabled=True,
    ),
    WORKSPACE_IDS["AUDIO"]: WorkspacePolicy(
        workspace_id=WORKSPACE_IDS["AUDIO"],
        root_path=HOME_PATH / "AgentIngress" / WORKSPACE_IDS["AUDIO"],
        allowed_tools=[],
    ),
    WORKSPACE_IDS["MEDICAL"]: WorkspacePolicy(
        workspace_id=WORKSPACE_IDS["MEDICAL"],
        root_path=HOME_PATH / "AgentIngress" / WORKSPACE_IDS["MEDICAL"],
        allowed_tools=[],
        embed_medical_outputs=False,
    ),
    WORKSPACE_IDS["TRAINING"]: WorkspacePolicy(
        workspace_id=WORKSPACE_IDS["TRAINING"],
        root_path=HOME_PATH / LOCAL_AGENT_STATE_DIR_NAME / "training_exports",
        allowed_tools=[],
    ),
}


class PolicyStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._policies = self._load_policies()

    def _load_policies(self) -> dict[str, WorkspacePolicy]:
        configured = self.settings.load_toml(self.settings.workspace_policy_path)
        policies = dict(DEFAULT_WORKSPACE_POLICIES)
        for item in configured.get("workspaces", []):
            policy = WorkspacePolicy.model_validate(item)
            policy = policy.model_copy(update={"root_path": policy.root_path.expanduser()})
            policies[policy.workspace_id] = policy
        return policies

    def all(self) -> list[WorkspacePolicy]:
        return list(self._policies.values())

    def get(self, workspace_id: str) -> WorkspacePolicy:
        try:
            return self._policies[workspace_id]
        except KeyError as exc:
            raise PermissionError(f"Unknown workspace_id: {workspace_id}") from exc

    def ensure_tool_allowed(self, workspace_id: str, tool_name: str) -> None:
        policy = self.get(workspace_id)
        if tool_name in policy.forbidden_tools or tool_name not in policy.allowed_tools:
            raise PermissionError(f"Tool {tool_name} is not allowed in workspace {workspace_id}")

    def ensure_path_in_workspace(self, workspace_id: str, path: Path) -> None:
        policy = self.get(workspace_id)
        root = policy.root_path.expanduser().resolve()
        candidate = path.expanduser().resolve()
        if root == HOME_PATH.resolve():
            return
        if root not in (candidate, *candidate.parents):
            raise PermissionError(f"Path {candidate} is outside workspace root {root}")

    def ensure_workflowy_parent_allowed(self, workspace_id: str, parent_node_id: str) -> None:
        policy = self.get(workspace_id)
        if not policy.write_enabled:
            raise PermissionError(f"Workflowy writes disabled for workspace {workspace_id}")
        if parent_node_id not in policy.approved_workflowy_parent_ids:
            raise PermissionError(f"Workflowy parent {parent_node_id} is not approved")


def seed_workspace_rows(policy_store: PolicyStore, repository: object) -> None:
    for policy in policy_store.all():
        repository.upsert_workspace(policy)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Saga policy engine
# ---------------------------------------------------------------------------


class PolicyViolation(StrEnum):
    NO_EXTERNAL_COMMS = "no_external_comms_without_approval"
    NO_PURCHASE = "no_purchase_without_approval"
    # The value still says "without_claim" and the mechanism it names is gone,
    # archived to potential_directions/file_claims/. The string is left alone
    # deliberately: it is persisted vocabulary, reachable in grant rows through
    # `granted_violations_for`, so renaming it is a migration and not a tidy-up.
    # What the class means today is "this agent may not edit files", which is
    # decided by the spawn posture rather than by any lock.
    NO_FILE_EDIT = "no_file_edit_without_claim"
    NO_CODE_MERGE = "no_code_merge_without_review"
    NO_MODEL_ESCALATION = "no_model_escalation_without_budget_reason"


@dataclass(frozen=True)
class PolicyVerdict:
    allowed: bool
    violation: PolicyViolation | None = None
    reason: str = ""
    requires_approval: bool = False


# Tools that send data outside the local environment
_EXTERNAL_COMMS_TOOLS = frozenset(
    {
        "send_email",
        "send_slack",
        "post_webhook",
        "http_request",
        "curl",
        "raw_http",
        "post_to_github",
        "create_pr",
    }
)

# Tools that interact with payment / purchase systems
_PURCHASE_TOOLS = frozenset(
    {
        "stripe_charge",
        "purchase",
        "pay",
        "subscribe",
        "checkout",
        "create_subscription",
        "add_payment_method",
    }
)

# Tools that merge code without a review gate
_MERGE_TOOLS = frozenset(
    {
        "git_merge",
        "git_push_main",
        "merge_pr",
        "squash_and_merge",
        "approve_and_merge",
    }
)

# Frontier models that cost more
_ESCALATION_MODELS = frozenset(
    {
        "claude-opus",
        "gpt-4",
        "gpt-4o",
        "o1",
        "o3",
        "claude-opus-4",
        "gemini-ultra",
    }
)

# Roles that SOUND powerful but must not bypass policies
_EXECUTIVE_ROLES = frozenset(
    {
        "ceo_agent",
        "cfo_agent",
        "cto_agent",
        "purchase_board",
        "product_marketer",
        "all_oracle",
    }
)


class SagaPolicyEngine:
    """Five hard-wired saga safety rules.

    Rules:
      1. No external comms without EXTERNAL_COMMS approval.
      2. No purchase/payment without PURCHASE approval.
      3. No file edit by a role that was not granted one.
      4. No code merge without a CODE_MERGE approval (implies prior review).
      5. No model escalation without MODEL_ESCALATION approval + budget reason.

    Critical invariant: roles do NOT imply permissions.
    An "executive" role can recommend — it cannot execute gated actions.
    """

    def check_tool_call(
        self,
        tool_name: str,
        agent_role: str,
        approved_actions: set[str] | None = None,
        budget_reason: str | None = None,
        model_name: str | None = None,
    ) -> PolicyVerdict:
        """Check whether a tool call is permitted.

        approved_actions: set of PolicyViolation values that have been
            explicitly approved for this saga/task.
        """
        approved = approved_actions or set()

        # Rule 1: external comms
        if tool_name in _EXTERNAL_COMMS_TOOLS and PolicyViolation.NO_EXTERNAL_COMMS not in approved:
            return PolicyVerdict(
                allowed=False,
                violation=PolicyViolation.NO_EXTERNAL_COMMS,
                reason=(
                    f"Tool '{tool_name}' sends data externally — submit an EXTERNAL_COMMS "
                    "approval request first."
                ),
                requires_approval=True,
            )

        # Rule 2: purchase
        if tool_name in _PURCHASE_TOOLS and PolicyViolation.NO_PURCHASE not in approved:
            return PolicyVerdict(
                allowed=False,
                violation=PolicyViolation.NO_PURCHASE,
                reason=(
                    f"Tool '{tool_name}' initiates payment — submit a PURCHASE approval "
                    "request first."
                ),
                requires_approval=True,
            )

        # Rule 3: an executive role does not get to edit files by being senior.
        #
        # This used to say the rule was "enforced by assert_claimed in the MCP
        # layer", which was never true: dispatched agents write with their own
        # harness tools and reach no coordination surface, so nothing checked a
        # claim before an edit. Claims have since been archived to
        # potential_directions/file_claims/. What actually holds here is this
        # verdict plus the spawn posture, which decides whether an agent's
        # harness may edit at all.
        if (
            agent_role.lower() in _EXECUTIVE_ROLES
            and tool_name in ("write_file", "edit_file", "patch")
            and PolicyViolation.NO_FILE_EDIT not in approved
        ):
            return PolicyVerdict(
                allowed=False,
                violation=PolicyViolation.NO_FILE_EDIT,
                reason=f"Role '{agent_role}' may not edit files (no role bypass).",
                requires_approval=False,
            )

        # Rule 4: code merge
        if tool_name in _MERGE_TOOLS and PolicyViolation.NO_CODE_MERGE not in approved:
            return PolicyVerdict(
                allowed=False,
                violation=PolicyViolation.NO_CODE_MERGE,
                reason=(
                    f"Tool '{tool_name}' merges code — submit a CODE_MERGE approval request "
                    "and ensure review is complete."
                ),
                requires_approval=True,
            )

        # Rule 5: model escalation
        if (
            model_name
            and any(m in model_name.lower() for m in _ESCALATION_MODELS)
            and PolicyViolation.NO_MODEL_ESCALATION not in approved
            and not budget_reason
        ):
            return PolicyVerdict(
                allowed=False,
                violation=PolicyViolation.NO_MODEL_ESCALATION,
                reason=(
                    f"Model '{model_name}' is frontier-tier — provide a budget_reason and get "
                    "MODEL_ESCALATION approval."
                ),
                requires_approval=True,
            )

        return PolicyVerdict(allowed=True)

    def check_role_boundary(self, agent_role: str, action: str) -> PolicyVerdict:
        """Enforce that executive/special roles cannot directly execute gated actions.

        CEO agent can recommend a purchase — it cannot call stripe_charge.
        Product Marketer can draft outbound copy — it cannot call send_email.
        """
        role_lower = agent_role.lower()
        if role_lower in _EXECUTIVE_ROLES:
            executive_blocked = _EXTERNAL_COMMS_TOOLS | _PURCHASE_TOOLS | _MERGE_TOOLS
            if action in executive_blocked:
                return PolicyVerdict(
                    allowed=False,
                    violation=PolicyViolation.NO_PURCHASE
                    if action in _PURCHASE_TOOLS
                    else PolicyViolation.NO_EXTERNAL_COMMS,
                    reason=(
                        f"Role '{agent_role}' can recommend but not execute '{action}'. "
                        "Submit an approval request and let an approved agent execute."
                    ),
                    requires_approval=True,
                )
        return PolicyVerdict(allowed=True)


# Module-level singleton
_SAGA_POLICY = SagaPolicyEngine()


def get_saga_policy() -> SagaPolicyEngine:
    return _SAGA_POLICY
