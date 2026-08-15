# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The wire between the grant ledger and the policy engine.

Both halves have existed for months and never met. `request_tool_permission` and
`grant_tool_permission` write durable, audited rows saying an agent may use a
capability. `SagaPolicyEngine.check_tool_call` decides whether an action needs an
approval and takes an `approved_actions` set saying which approvals are in hand.
Nothing ever read a granted row, and nothing ever filled that parameter, so an
agent could ask, an operator could grant, and the code did what it was going to
do regardless.

This module is the missing read and the missing call. It is deliberately small:
the durable state, the rules, and the vocabulary all already exist, and adding a
sixth place that decides what an agent may do would be repeating the mistake
that made this necessary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from .access_posture import AccessPosture, record_unenforced_refusal, resolve
from .capabilities import Capability, gated_capabilities, violation_cleared_by
from .coordination.store import connect, now
from .policies import get_saga_policy
from .policy_document import CompiledPolicy, load_policy_document
from .settings import get_settings
from .staffing import Tier, load_bench


def _require_pow_wow_id(pow_wow_id: str) -> str:
    """Return a usable grant scope or reject the programmer error."""

    if not pow_wow_id:
        raise ValueError("pow_wow_id is required for an agent capability check")
    return pow_wow_id


def policy_principal(agent_name: str, agent_role: str, policy: CompiledPolicy) -> str:
    """Which section of `POLICIES.md` governs this caller.

    The seat, when the document names one. What a grant belongs to is the seat a
    harness holds, not the vendor holding it: an implementer writes code and runs
    commands because it is the implementer, and the same vendor reviewing must
    not. Keying the document on the vendor said that only by coincidence, and the
    coincidence had to be maintained by hand - a staffing swap meant editing
    `POLICIES.md`, re-pinning its hash, and correcting prose, or else the gate
    denied the implementer the write its plan had already granted. That is a
    static rule breaking a modular seat, and it cost a demo more than once.

    The role is asked before the bench because a vendor can hold two seats at
    once, which is what an outage staffing looks like. Seat-named roles come from
    the compiled plan; a role naming no seat falls through to the bench, which
    answers only when the vendor holds exactly one.

    Vendor-named sections still work and are checked last, so a document can be
    migrated one principal at a time and an operator who wants to pin a specific
    vendor still can.
    """

    role = agent_role.strip().lower()
    if role in policy.principals:
        return role
    try:
        tier = Tier(role)
    except ValueError:
        tier = None
    if tier is not None and tier.value in policy.principals:
        return tier.value
    bench = load_bench(get_settings().config_dir / "staffing.toml")
    seats = [tier for tier, slot in bench.items() if slot.harness.value == agent_name]
    if len(seats) == 1 and seats[0].value in policy.principals:
        return seats[0].value
    return agent_name


@dataclass(frozen=True)
class SystemWorkflow:
    """The application acting on its own behalf, processing an ingress event.

    Not an agent and not a bypass. An ingress workflow has no principal to check
    an ACL against - `IngressEvent` carries a source and a workspace, never a
    caller - and inventing one would make the ACL answer a question nobody asked.
    Its bound is the workspace policy, which is a statement about *where* rather
    than *who*, and that is the right bound for the system's own hands.
    """

    source: str


@dataclass(frozen=True)
class AgentCaller:
    """A named principal whose grants decide what it may do.

    ``pow_wow_id`` is required. It used to default to ``None``, and the old
    unscoped grant read widened to *every* grant that agent name had ever been
    given, in any pow-wow. A caller believed it was asking "may this agent do
    this here" and was asking "has this agent ever been allowed this anywhere".

    A default that silently widens a permission check is worse than no default,
    and the way to stop a caller from taking it is to not offer it. Every
    principal is constructed where a pow-wow is in scope, so nothing has to
    invent one.
    """

    agent_name: str
    agent_role: str
    pow_wow_id: str

    def __post_init__(self) -> None:
        _require_pow_wow_id(self.pow_wow_id)


# Who is asking. Two variants rather than an optional agent name, so a call site
# has to say which it is: an absent name would otherwise read as "system" by
# accident, and the difference decides whether an ACL is consulted at all.
CallerIdentity = SystemWorkflow | AgentCaller


@dataclass(frozen=True)
class CapabilityGranted:
    """The action may proceed."""


@dataclass(frozen=True)
class CapabilityDenied:
    """The action may not proceed, and what would change that.

    Carries the request path rather than only a refusal, because the agent that
    hit this can act on it: every denial here is a capability an operator can
    grant, and a message that does not say so turns a gate into a dead end.
    """

    capability: Capability
    reason: str

    @property
    def remedy(self) -> str:
        return (
            f"request it with request_tool_permission("
            f"tool_name={self.capability.value!r}, reason=...) "
            "and have an operator run grant_tool_permission"
        )


CapabilityVerdict = CapabilityGranted | CapabilityDenied


def granted_violations_for(agent_name: str, pow_wow_id: str) -> set[str]:
    """Which approvals this agent currently holds, as the policy engine spells them.

    Always scoped to one pow-wow. A grant made for one piece of work should not
    silently authorize the next, and an operator-wide grant inventory belongs
    in the coordination ledger's list surface rather than in an enforcement
    query.

    An expired grant is not a grant. Filtered here in SQL rather than compared
    after the fact, so there is no window in which a caller holds a row it then
    has to remember to check: the query cannot return one. ``expires_at IS NULL``
    is a standing grant, which is what every row written before the column meant
    and is now something somebody chose.

    The type rejects omitted scope and the value check rejects untyped callers
    that pass ``None`` or an empty string. Both are programmer errors because an
    agent grant has no safe unscoped meaning.
    """

    scope = _require_pow_wow_id(pow_wow_id)
    with connect() as connection:
        rows = connection.execute(
            "SELECT tool_name FROM tool_permission_requests "
            "WHERE agent_name = ? AND pow_wow_id = ? AND status = ? "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (agent_name, scope, "GRANTED", now()),
        ).fetchall()

    cleared: set[str] = set()
    for row in rows:
        try:
            capability = Capability(str(row["tool_name"]))
        except ValueError:
            # A row predating the typed column. It cannot clear anything, and
            # skipping it is the fail-closed reading.
            continue
        violation = violation_cleared_by(capability)
        if violation is not None:
            cleared.add(violation)
    return cleared


def revoked_capabilities_for(agent_name: str, pow_wow_id: str) -> set[Capability]:
    """Which capabilities have been taken back from this agent for this pow-wow.

    The ledger's job at spawn time is revocation, not granting. The compiled plan
    is the grant: an operator approved the DesignDoc, the compiler bound a
    capability set to each executor kind, the plan hash is immutable, and
    `SpawnAuthority.narrowed_to` already makes it impossible for a task to hold
    anything the plan did not give it. Asking an operator to hand-grant the same
    capabilities a second time would teach them to grant reflexively, which buys
    nothing and costs the meaning of the word.

    What the plan cannot express is a change of mind *while the work is running*.
    That is this table, and this function is how the gate hears about it.
    """

    with connect() as connection:
        rows = connection.execute(
            "SELECT tool_name FROM tool_permission_requests "
            "WHERE agent_name = ? AND pow_wow_id = ? AND status = ?",
            (agent_name, pow_wow_id, "REVOKED"),
        ).fetchall()
    revoked: set[Capability] = set()
    for row in rows:
        try:
            revoked.add(Capability(str(row["tool_name"])))
        except ValueError:
            # A row naming something no capability answers to cannot revoke
            # anything, because nothing could have granted it either.
            continue
    return revoked


def check_capability(
    *,
    agent_name: str,
    agent_role: str,
    capability: Capability,
    pow_wow_id: str,
) -> CapabilityVerdict:
    """Whether this agent may use this capability right now.

    Three questions, and they are different refusals.

    **Does the written policy permit it?** `POLICIES.md` is the
    operator's own statement of who may do what, compiled into an immutable
    hashed revision. It is asked first and it outranks everything below: a
    `Never:` line cannot be lifted by a grant, which is the property that makes
    writing one worth the trouble.

    **Has it been taken back?** A gated capability - one `_CLEARS` names, meaning
    it implies an approval class - is refused if this pow-wow has a `REVOKED` row
    for it. This is the check that was missing, and its absence was not obvious:
    the function *looked* like enforcement because it called the policy engine,
    but it passed `capability.value` in as a **tool name**, and `check_tool_call`
    matches tool names against hardcoded sets like `send_email` and `git_merge`.
    No `Capability` value appears in any of them, so every capability was allowed
    always, whatever the ledger said. A check that cannot fail is not a check.

    **Does the policy forbid it anyway?** In principle. In practice this leg
    decides nothing and the honest thing is to say so here rather than let the
    call read as a third opinion. No `Capability` value matches any of
    `check_tool_call`'s rule sets, including for the members that *are* registry
    tool names, so it returns allowed every time;
    `test_no_capability_value_is_a_tool_name_the_policy_rules_match` pins that.

    It is left in place rather than deleted because the fix is not the obvious
    one. Making a capability value match a rule would switch on a widening:
    `_CLEARS` is many-to-one and `granted_violations_for` returns approval
    *classes*, so a grant of `workflowy_day_bullet_insert` would clear
    `NO_EXTERNAL_COMMS` for `access_credentials` and `publish_deployment` as
    well. That has to be resolved before this leg is worth waking up.

    Deliberately *not* asking "is there a grant". The plan is the grant, and
    requiring a second one would deny every implementation milestone on a
    correctly compiled plan.

    Scope worth stating plainly: the only production caller is `_authorize_spawn`
    in `pow_wow/executor.py`, over `authority.capabilities & gated_capabilities()`.
    A capability no `ExecutorDeclaration` grants is never asked about here, and
    `ToolRegistry` dispatch does not come through this function at all.
    """

    scope = _require_pow_wow_id(pow_wow_id)
    posture = resolve(get_settings().access_posture, capability)

    def refuse(reason: str) -> CapabilityVerdict:
        """One exit for every denial, so the posture cannot be applied unevenly.

        Every refusal in this function goes through here. That is the point: a
        second `return CapabilityDenied(...)` added later would silently be
        posture-blind, and the failure mode of a permissive posture is a refusal
        that ignores it rather than one that honours it too eagerly.
        """

        match posture:
            case AccessPosture.ENFORCING:
                return CapabilityDenied(capability=capability, reason=reason)
            case AccessPosture.OBSERVING:
                record_unenforced_refusal(
                    capability=capability,
                    agent_name=agent_name,
                    pow_wow_id=scope,
                    reason=reason,
                )
                return CapabilityGranted()
            case _:
                assert_never(posture)

    written = load_policy_document()
    principal = policy_principal(agent_name, agent_role, written)
    if not written.permits(principal, capability):
        # The written document outranks both the compiled plan and the ledger,
        # and it is checked first so nothing downstream can reach past it. An
        # agent that could ask its way around `POLICIES.md` at runtime
        # would make the document advisory, and an advisory policy is a comment.
        return refuse(
            f"POLICIES.md does not permit {capability.value!r} for principal "
            f"{principal!r} (agent {agent_name!r} acting as {agent_role!r})"
        )
    if capability in gated_capabilities() and capability in revoked_capabilities_for(
        agent_name, scope
    ):
        return refuse(
            f"{capability.value!r} was revoked for agent {agent_name!r} in pow-wow {scope}"
        )
    verdict = get_saga_policy().check_tool_call(
        capability.value,
        agent_role,
        approved_actions=granted_violations_for(agent_name, scope),
    )
    if verdict.allowed:
        return CapabilityGranted()
    return refuse(verdict.reason or "denied by policy")


def ensure_capability(
    *,
    agent_name: str,
    agent_role: str,
    capability: Capability,
    pow_wow_id: str,
) -> None:
    """Raise unless this agent may use this capability.

    A denial is a hard failure rather than a logged warning, per the design rule
    that a crash gets fixed and a severe-log does not. An agent proceeding past a
    denied capability is the exact condition this module exists to make
    impossible.
    """

    verdict = check_capability(
        agent_name=agent_name,
        agent_role=agent_role,
        capability=capability,
        pow_wow_id=pow_wow_id,
    )
    if isinstance(verdict, CapabilityDenied):
        raise PermissionError(f"{verdict.reason} -- {verdict.remedy}")


def ensure_caller_may_use(caller: CallerIdentity, tool_name: str) -> None:
    """The tool-boundary check: may this caller use this tool?

    One function for every path into a tool, which is the point. The approved
    parent gate lives on the `/workflowy/write` route, so anything reaching the
    same tool another way skipped it entirely; a check that sits at the boundary
    cannot be gone around by arriving from somewhere else.

    A `SystemWorkflow` passes. Not because it lacks an identity - it carries the
    ingress source it is acting for, and every actor has one - but because the
    policy today is that the application's own ingress handling is trusted, and
    its bound is the workspace. Writing that as a variant rather than as an
    absent name is what makes it a decision someone can later revoke: giving
    system identities real ACL entries means adding a lookup here, not finding a
    principal that was never recorded.

    A tool name no capability answers to cannot be granted to anyone, so an agent
    is refused it and the system path is left to the registry's own lookup. That
    keeps a test fixture or an unregistered tool from silently becoming
    agent-reachable.
    """

    if isinstance(caller, SystemWorkflow):
        return
    try:
        capability = Capability(tool_name)
    except ValueError:
        raise PermissionError(
            f"{caller.agent_name} may not use {tool_name!r}: no capability by that name "
            "exists, so it cannot be granted"
        ) from None
    ensure_capability(
        agent_name=caller.agent_name,
        agent_role=caller.agent_role,
        capability=capability,
        pow_wow_id=caller.pow_wow_id,
    )


__all__ = [
    "AgentCaller",
    "CallerIdentity",
    "CapabilityDenied",
    "CapabilityGranted",
    "CapabilityVerdict",
    "SystemWorkflow",
    "check_capability",
    "ensure_capability",
    "ensure_caller_may_use",
    "granted_violations_for",
]
