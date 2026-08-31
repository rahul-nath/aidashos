# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""CLI and MCP adapters over the packaged coordination lifecycle modules.

Provides two layers of coordination:
  Layer 1 (file-level): sessions, claims, notes, handoffs — unchanged from v1.
  Layer 2 (work-unit): sagas, pow-wows, tasks, artifacts, tool-permission
    requests, evaluation results, approval gates, and GAWD docs.

The command surface uses Postgres for the authoritative runtime ledger.
The SQLite adapter is retained only for isolated tests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal

from local_first_agent_os.constants import (
    APPROVAL_REQUEST_TYPES,
    DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
)

from ..refinery.loop import run_refinery, run_refinery_fleet
from ..work_units import commands as work_unit_commands
from ..work_units.next_commands import (
    NextCommandSet,
    NextCommandStatus,
    next_commands_for,
)
from .approvals import (
    list_approval_requests,
    resolve_approval_request,
    revoke_approval_request,
    submit_approval_request,
)
from .availability import ledger_unavailable
from .checkpoints import (
    append_execution_event,
    attach_execution_artifact,
    create_execution_checkpoint,
    decide_execution_checkpoint,
    get_execution_checkpoint,
    list_execution_artifacts,
    list_execution_checkpoints,
    list_execution_events,
    request_recovery_staff_review,
)
from .collaboration import (
    append_note,
    handoff,
    heartbeat,
    list_sessions,
    read_notes,
    register_agent,
)
from .contracts import DispatchKind
from .dispatch import (
    cancel_dispatch_intent,
    claim_next_dispatch_intent,
    complete_dispatch_intent,
    list_dispatch_intents,
    submit_dispatch_intent,
    supersede_dispatch_intent,
)
from .dispatcher_loop import run_ledger_dispatcher
from .doctrine_staleness import list_doctrine_stale_reviews
from .execution import (
    LEASE_TERMINAL_STATUSES,
    WORK_ABANDONED_AFTER_SECONDS,
    claim_next_ledger_event,
    complete_execution_lease,
    complete_ledger_event,
    gc_ledger,
    heartbeat_execution_lease,
    list_execution_leases,
    list_ledger_events,
    open_execution_lease,
    request_execution_cancel,
)
from .execution_ledger import read_execution_ledger
from .frontier_usage import (
    find_compatible_agent_continuation,
    list_frontier_usage_records,
)
from .integration_queue import list_integration_requests
from .machine_readiness import run_first_run_check
from .milestones import (
    MILESTONE_EVIDENCE_TYPES,
    amend_saga_milestone,
    complete_saga_milestone,
    create_saga_milestone,
    fail_saga_milestone,
    get_saga_milestone,
    list_saga_milestones,
    next_ready_saga_milestone,
    reconcile_saga_milestones,
    record_milestone_evidence,
    retry_saga_milestone,
    start_saga_milestone,
)
from .outcomes import TerminalOutcome
from .pow_wows import (
    claim_task,
    complete_pow_wow,
    complete_task,
    create_pow_wow,
    delegate_task,
    deny_tool_permission,
    evaluate_artifact,
    fail_task,
    get_artifact,
    get_evaluation_summary,
    get_pow_wow,
    grant_tool_permission,
    join_pow_wow,
    latest_repo_audit,
    list_pow_wows,
    list_tasks,
    list_tool_permission_requests,
    request_tool_permission,
    restore_tool_permission,
    revoke_tool_permission,
    submit_artifact,
)
from .projects import (
    approve_gawd_doc,
    attach_gawd_doc_to_saga,
    check_ambiguity,
    check_stagnation,
    complete_saga,
    create_gawd_doc,
    create_saga,
    get_gawd_doc,
    get_saga,
    list_sagas,
    supersede_gawd_doc,
)
from .resident_loop import describe_resident_loops
from .review_recovery import recover_unparsed_staff_review
from .store import (
    migrate_postgres_schema,
    ok,
    set_root,
)


def print_json(x: Any) -> None:
    print(json.dumps(x, indent=2, sort_keys=True))


# The width of the rule above the suggestions. Narrow enough to survive an
# 80-column terminal, which is what a laptop running the watch loop beside a
# cockpit actually has.
_NEXT_COMMAND_RULE_WIDTH: Final = 72


def render_next_commands(result: NextCommandSet) -> str:
    """The suggestions as an operator reads them: what works, then what does not.

    Grouped by status rather than listed flat, because the grouping *is* the
    message. A flat list asks the operator to read every precondition to find the
    runnable command; these headings answer that before they read a single line.

    The refused group is not noise. It exists because the recovery verbs read
    alike, and an operator who does not see `adopt_settled_work_unit_dispatch`
    ruled out here will reach for it themselves and spend a command learning what
    this already knows.

    **The whole block is valid shell.** Every explanatory line is a `#` comment,
    and a command that is not READY is itself commented out. Indentation alone
    used to carry that distinction, and it does not survive a long command
    wrapping in an 80-column terminal: the first reader of this output asked
    whether a command and the `but` line under it were one command. Now the
    question cannot arise, and pasting a whole group runs only what was runnable
    - a refused verb pasted by accident is a comment rather than a failed call.
    """

    lines = [
        "",
        "# ── next commands " + "─" * (_NEXT_COMMAND_RULE_WIDTH - 18),
        f"# {result.headline}",
    ]
    if result.detail:
        lines.append(f"#   {result.detail}")

    groups = (
        (NextCommandStatus.READY, "ready - runnable exactly as printed"),
        (NextCommandStatus.REFUSED, "refused in this state - commented out"),
        (NextCommandStatus.UNPROVED, "needs a fact you supply - commented out"),
    )
    for status, heading in groups:
        members = [item for item in result.commands if item.status is status]
        if not members:
            continue
        lines.append("")
        lines.append(f"# {heading}")
        for item in members:
            lines.append("")
            lines.append(f"  # {item.intent}")
            if item.precondition and status is not NextCommandStatus.READY:
                lines.append(f"  #   needs  {item.precondition}")
            if item.reason:
                code = f"  ->  {item.refusal_code}" if item.refusal_code else ""
                lines.append(f"  #   but    {item.reason}{code}")
            # The command last, directly above the next blank line, so the thing
            # to copy is the final line of its block rather than buried above
            # three lines of prose.
            prefix = "  " if status is NextCommandStatus.READY else "  # "
            lines.append(f"{prefix}{item.command}")
    return "\n".join(lines)


def print_next_commands(cmd: str, payload: Mapping[str, Any]) -> None:
    """Print the follow-up commands to stderr, or nothing when there are none.

    Stderr rather than stdout, and deliberately so. Every documented way of
    watching this system pipes stdout into a JSON parser - the handoff's own
    watch loop is `get_work_unit ... | python3 -c 'json.load(sys.stdin)'` - and
    text on stdout would break all of them. Stderr puts the commands on the
    operator's terminal, beside the JSON, while leaving the document they piped
    exactly as it was.

    It is also why this lives in `main` rather than in the payload. The
    in-process transport serves resident loops and dispatched agents, which do
    not copy and paste and do pay for every token; a field in the envelope would
    charge them for an affordance only a human at a terminal can use.

    A failure here must never take down the command that succeeded. The result is
    already on stdout and already correct; a defect in a convenience is not
    grounds for a non-zero exit, and the traceback goes to stderr where an
    operator will see it.
    """

    try:
        result = next_commands_for(cmd, payload)
        if result is None:
            return
        # The JSON is already written but not necessarily flushed, and stdout is
        # block-buffered whenever it is not a terminal. Without this the two
        # streams interleave backwards under `2>&1` - suggestions first, then the
        # result they are about - which is exactly how an operator redirecting to
        # a log file would read them.
        sys.stdout.flush()
        print(render_next_commands(result), file=sys.stderr)
    except Exception as failure:  # pragma: no cover - defensive
        print(
            f"(next commands unavailable: {type(failure).__name__}: {failure})",
            file=sys.stderr,
        )


# What a dispatched agent may ask the ledger, and nothing else.
#
# Read-only is the whole property. An agent that could write here could file
# evidence about its own run, and the verification gate and the cross-provider
# review both rest on an agent's account of itself not being evidence. Because
# nothing in this set writes, the surface needs no approval gate of its own.
#
# The three answer the questions a dispatched agent otherwise guesses at:
# what has actually executed (the one `skills/agent-startup/SKILL.md` calls a
# query rather than an inference), who owns the resident loops right now, and
# what work is queued or already claimed.
#
# Deliberately not here: the claim verbs, which the 2026-08-06 handoff argues
# against wiring at all and which the integration queue does not need, since it
# reads `changed_files` from git after a run; and every approval verb, because
# an agent resolving its own CODE_MERGE is the failure the gates exist for.
AGENT_READABLE_TOOLS: Final = (
    "read_execution_ledger",
    "describe_resident_loops",
    "list_dispatch_intents",
)


def build_agent_read_mcp_server():
    """The ledger as a dispatched agent sees it: three questions, no answers back.

    A separate server rather than a filter over the full one, because the safety
    property here is the tool list itself. Building the 93-tool server and
    removing things would make the guarantee depend on the removal staying
    complete as tools are added; building up from an explicit tuple means a new
    tool is absent until somebody names it.
    """

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise SystemExit('Missing MCP SDK. Install with: pip install "mcp[cli]"') from e

    mcp = FastMCP(
        "agent_coordination_read",
        instructions=(
            "Read-only view of this system's durable coordination ledger.\n\n"
            "Use it to establish what has already happened rather than inferring it:\n"
            "  read_execution_ledger   - what workflows and steps have actually run\n"
            "  describe_resident_loops - which process owns each resident loop, and at "
            "which revision\n"
            "  list_dispatch_intents   - what work is queued, claimed, or terminal\n\n"
            "Nothing here writes. Your own work is reported through the diff you leave "
            "in your worktree, not through this server."
        ),
    )
    exposed = {
        "read_execution_ledger": read_execution_ledger,
        "describe_resident_loops": describe_resident_loops,
        "list_dispatch_intents": list_dispatch_intents,
    }
    assert set(exposed) == set(AGENT_READABLE_TOOLS)
    for name, handler in exposed.items():
        mcp.tool(name=name)(handler)
    return mcp


def build_mcp_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise SystemExit('Missing MCP SDK. Install with: pip install "mcp[cli]"') from e

    mcp = FastMCP(
        "agent_coordination",
        instructions=(
            "Durable repo-local coordination server for multiple agents.\n\n"
            "LAYER 1 — file coordination: register session → claim files → edit → release.\n"
            "LAYER 2 — work-unit coordination:\n"
            "  1. create_gawd_doc → approve_gawd_doc (immutable spec)\n"
            "  2. check_ambiguity → must pass before execution\n"
            "  3. create_saga → create_saga_milestone (milestones drive the saga)\n"
            "  4. create_pow_wow → join_pow_wow (role ≠ permissions!)\n"
            "  5. claim_task → submit_artifact → evaluate_artifact\n"
            "  6. request_tool_permission (for any sensitive tool)\n"
            "  7. submit_approval_request for: PURCHASE / EXTERNAL_COMMS / "
            "CODE_MERGE / MODEL_ESCALATION / REVIEW_ESCALATION\n"
            "  8. complete_pow_wow → check_stagnation → complete_saga\n\n"
            "POLICY: roles do not imply permissions. A CEO agent can recommend; it cannot pay. "
            "A Staff Engineer can approve a plan; it cannot bypass file claims.\n\n"
            "Start with run_first_run_check when the machine's readiness is in question: "
            "it reports what is missing and the exact command that fixes each miss."
        ),
    )

    # Layer 1
    mcp.tool(name="register_agent")(register_agent)
    mcp.tool(name="heartbeat")(heartbeat)
    mcp.tool(name="append_note")(append_note)
    mcp.tool(name="read_notes")(read_notes)
    mcp.tool(name="handoff")(handoff)
    mcp.tool(name="list_sessions")(list_sessions)

    # Layer 3 — DesignDoc-governed WorkUnits
    mcp.tool(name="compile_design_doc")(work_unit_commands.compile_design_doc)
    mcp.tool(name="start_work_unit")(work_unit_commands.start_work_unit)
    mcp.tool(name="get_work_unit")(work_unit_commands.get_work_unit)
    mcp.tool(name="list_work_units")(work_unit_commands.list_work_units)
    mcp.tool(name="list_design_docs")(work_unit_commands.list_design_docs)
    mcp.tool(name="list_work_unit_events")(work_unit_commands.list_work_unit_events)
    mcp.tool(name="list_work_unit_artifacts")(work_unit_commands.list_work_unit_artifacts)
    mcp.tool(name="submit_work_unit_decision")(work_unit_commands.submit_work_unit_decision)
    mcp.tool(name="cancel_work_unit")(work_unit_commands.cancel_work_unit)
    mcp.tool(name="resume_work_unit")(work_unit_commands.resume_work_unit)
    mcp.tool(name="adopt_recovered_work_unit_dispatch")(
        work_unit_commands.adopt_recovered_work_unit_dispatch
    )
    mcp.tool(name="adopt_integrated_work_unit_milestone")(
        work_unit_commands.adopt_integrated_work_unit_milestone
    )
    mcp.tool(name="adopt_settled_work_unit_dispatch")(
        work_unit_commands.adopt_settled_work_unit_dispatch
    )
    mcp.tool(name="run_enqueue_drainer")(work_unit_commands.run_enqueue_drainer)
    mcp.tool(name="run_crash_reconciler")(work_unit_commands.run_crash_reconciler)

    # Layer 2 — durable execution history
    mcp.tool(name="read_execution_ledger")(read_execution_ledger)
    mcp.tool(name="run_ledger_dispatcher")(run_ledger_dispatcher)
    mcp.tool(name="describe_resident_loops")(describe_resident_loops)

    # Machine readiness. The one question every onboarding conversation starts
    # with, adapted from scripts/first-run-check.sh for MCP clients that have no
    # shell. Operator surface only: a dispatched agent's machine is already
    # ready by the time it exists.
    mcp.tool(name="run_first_run_check")(run_first_run_check)

    # Layer 2 — GAWD docs
    mcp.tool(name="create_gawd_doc")(create_gawd_doc)
    mcp.tool(name="approve_gawd_doc")(approve_gawd_doc)
    mcp.tool(name="supersede_gawd_doc")(supersede_gawd_doc)
    mcp.tool(name="get_gawd_doc")(get_gawd_doc)
    mcp.tool(name="attach_gawd_doc_to_saga")(attach_gawd_doc_to_saga)
    mcp.tool(name="check_ambiguity")(check_ambiguity)

    # Layer 2 — Sagas
    mcp.tool(name="create_saga")(create_saga)
    mcp.tool(name="get_saga")(get_saga)
    mcp.tool(name="list_sagas")(list_sagas)
    mcp.tool(name="complete_saga")(complete_saga)
    mcp.tool(name="check_stagnation")(check_stagnation)

    # Layer 2 — Saga milestones
    mcp.tool(name="create_saga_milestone")(create_saga_milestone)
    mcp.tool(name="amend_saga_milestone")(amend_saga_milestone)
    mcp.tool(name="list_saga_milestones")(list_saga_milestones)
    mcp.tool(name="get_saga_milestone")(get_saga_milestone)
    mcp.tool(name="record_milestone_evidence")(record_milestone_evidence)
    mcp.tool(name="start_saga_milestone")(start_saga_milestone)
    mcp.tool(name="complete_saga_milestone")(complete_saga_milestone)
    mcp.tool(name="fail_saga_milestone")(fail_saga_milestone)
    mcp.tool(name="retry_saga_milestone")(retry_saga_milestone)
    mcp.tool(name="next_ready_saga_milestone")(next_ready_saga_milestone)
    mcp.tool(name="reconcile_saga_milestones")(reconcile_saga_milestones)

    # Layer 2 — Pow-wows
    mcp.tool(name="create_pow_wow")(create_pow_wow)
    mcp.tool(name="get_pow_wow")(get_pow_wow)
    mcp.tool(name="list_pow_wows")(list_pow_wows)
    mcp.tool(name="join_pow_wow")(join_pow_wow)
    mcp.tool(name="complete_pow_wow")(complete_pow_wow)

    # Layer 2 — Tasks
    mcp.tool(name="claim_task")(claim_task)
    mcp.tool(name="complete_task")(complete_task)
    mcp.tool(name="fail_task")(fail_task)
    mcp.tool(name="list_tasks")(list_tasks)
    mcp.tool(name="delegate_task")(delegate_task)

    # Layer 2 — Artifacts
    mcp.tool(name="submit_artifact")(submit_artifact)
    mcp.tool(name="get_artifact")(get_artifact)
    mcp.tool(name="latest_repo_audit")(latest_repo_audit)

    # Layer 2 — Tool permissions
    mcp.tool(name="request_tool_permission")(request_tool_permission)
    mcp.tool(name="grant_tool_permission")(grant_tool_permission)
    mcp.tool(name="deny_tool_permission")(deny_tool_permission)
    mcp.tool(name="revoke_tool_permission")(revoke_tool_permission)
    mcp.tool(name="restore_tool_permission")(restore_tool_permission)
    mcp.tool(name="list_tool_permission_requests")(list_tool_permission_requests)

    # Layer 2 — Evaluation
    mcp.tool(name="evaluate_artifact")(evaluate_artifact)
    mcp.tool(name="get_evaluation_summary")(get_evaluation_summary)

    # Layer 2 — Approval gates
    mcp.tool(name="submit_approval_request")(submit_approval_request)
    mcp.tool(name="resolve_approval_request")(resolve_approval_request)
    mcp.tool(name="revoke_approval_request")(revoke_approval_request)
    mcp.tool(name="list_approval_requests")(list_approval_requests)
    mcp.tool(name="recover_unparsed_staff_review")(recover_unparsed_staff_review)
    mcp.tool(name="list_integration_requests")(list_integration_requests)
    mcp.tool(name="run_refinery")(run_refinery)
    mcp.tool(name="run_refinery_fleet")(run_refinery_fleet)
    mcp.tool(name="submit_dispatch_intent")(submit_dispatch_intent)
    mcp.tool(name="claim_next_dispatch_intent")(claim_next_dispatch_intent)
    mcp.tool(name="complete_dispatch_intent")(complete_dispatch_intent)
    mcp.tool(name="cancel_dispatch_intent")(cancel_dispatch_intent)
    mcp.tool(name="supersede_dispatch_intent")(supersede_dispatch_intent)
    mcp.tool(name="list_dispatch_intents")(list_dispatch_intents)
    mcp.tool(name="list_doctrine_stale_reviews")(list_doctrine_stale_reviews)
    mcp.tool(name="open_execution_lease")(open_execution_lease)
    mcp.tool(name="heartbeat_execution_lease")(heartbeat_execution_lease)
    mcp.tool(name="request_execution_cancel")(request_execution_cancel)
    mcp.tool(name="complete_execution_lease")(complete_execution_lease)
    mcp.tool(name="list_execution_leases")(list_execution_leases)
    mcp.tool(name="append_execution_event")(append_execution_event)
    mcp.tool(name="list_execution_events")(list_execution_events)
    mcp.tool(name="find_agent_continuation")(find_compatible_agent_continuation)
    mcp.tool(name="list_frontier_usage_records")(list_frontier_usage_records)
    mcp.tool(name="attach_execution_artifact")(attach_execution_artifact)
    mcp.tool(name="list_execution_artifacts")(list_execution_artifacts)
    mcp.tool(name="create_execution_checkpoint")(create_execution_checkpoint)
    mcp.tool(name="get_execution_checkpoint")(get_execution_checkpoint)
    mcp.tool(name="list_execution_checkpoints")(list_execution_checkpoints)
    mcp.tool(name="decide_execution_checkpoint")(decide_execution_checkpoint)
    mcp.tool(name="request_recovery_staff_review")(request_recovery_staff_review)
    mcp.tool(name="claim_next_ledger_event")(claim_next_ledger_event)
    mcp.tool(name="complete_ledger_event")(complete_ledger_event)
    mcp.tool(name="list_ledger_events")(list_ledger_events)
    mcp.tool(name="gc_ledger")(gc_ledger)

    # Resources
    @mcp.resource("coordination://notes/{scope}")
    def notes_resource(scope: str) -> str:
        """Notes for a scope as JSON."""
        return json.dumps(read_notes(scope), indent=2, sort_keys=True)

    @mcp.resource("coordination://sagas")
    def sagas_resource() -> str:
        """All sagas as JSON."""
        return json.dumps(list_sagas(), indent=2, sort_keys=True)

    @mcp.resource("coordination://sagas/{saga_id}/pow_wows")
    def pow_wows_resource(saga_id: str) -> str:
        """Pow-wows for a saga as JSON."""
        return json.dumps(list_pow_wows(saga_id), indent=2, sort_keys=True)

    @mcp.resource("coordination://approvals/pending")
    def pending_approvals_resource() -> str:
        """Pending approval requests as JSON."""
        return json.dumps(list_approval_requests(status_filter="PENDING"), indent=2, sort_keys=True)

    return mcp


def serve_mcp(
    transport: Literal["stdio", "streamable-http"] = "stdio",
    *,
    audience: Literal["operator", "agent"] = "operator",
) -> None:
    """Run the coordination ledger as an MCP server.

    `audience` picks which surface the caller gets. An operator tool gets all of
    it; a dispatched agent gets `build_agent_read_mcp_server`, which is
    read-only. Nothing in this repository calls this function: a stdio server is
    started by the client that consumes it, so the executor writes a config
    naming this command and the agent's own harness spawns it if and when it
    asks a question.
    """

    mcp = build_agent_read_mcp_server() if audience == "agent" else build_mcp_server()
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport="streamable-http")


def build_parser() -> argparse.ArgumentParser:
    """The one grammar for coordination commands.

    Both transports parse with this. An in-process caller reusing the argv
    grammar costs one parse and keeps a single source of truth for what each
    command takes; a second dispatcher keyed on the command enum would be a
    second place for the two to disagree.

    Building it costs about 3ms of the in-process transport's ~10ms per command
    and is deliberately not cached: `--session` defaults to `AGENT_SESSION_ID`,
    which a cached parser would freeze at the first build for the life of a
    resident daemon. Move that default to parse time before caching this.
    """

    p = argparse.ArgumentParser(
        description="Durable repo-local MCP coordination server (file + work-unit layers)."
    )
    p.add_argument("--root", default=None, help="repo root; otherwise auto-detect .git or cwd")
    p.add_argument(
        "--no-next-commands",
        action="store_true",
        # Defaulted from the environment because, like every option on this
        # parser, the flag only parses before the subcommand. An operator who
        # wants the suggestions off for good should not have to remember argparse
        # ordering on every invocation. Read at parse time rather than captured,
        # for the reason the docstring above gives about `--session`.
        default=os.environ.get("LOCAL_AGENT_NO_NEXT_COMMANDS", "").strip().lower()
        in {"1", "true", "yes"},
        help=(
            "suppress the follow-up commands printed to stderr after a result; "
            "LOCAL_AGENT_NO_NEXT_COMMANDS=1 does the same for a whole shell"
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sess = argparse.ArgumentParser(add_help=False)
    sess.add_argument("--session", default=os.environ.get("AGENT_SESSION_ID"))

    # ---- serve ----
    sv = sub.add_parser("serve", help="run as an MCP server")
    sv.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    sv.add_argument(
        "--audience",
        choices=["operator", "agent"],
        default="operator",
        help="'agent' exposes only the read-only ledger view a dispatched agent gets",
    )

    # ---- layer 1 ----
    r = sub.add_parser("register_agent", aliases=["register-agent", "register"])
    r.add_argument("agent_name")
    r.add_argument("--session")

    hb = sub.add_parser("heartbeat")
    hb.add_argument("--session", required=True)

    sub.add_parser("list_sessions", aliases=["sessions"])

    an = sub.add_parser("append_note", aliases=["note"], parents=[sess])
    an.add_argument("scope")
    an.add_argument("message")

    rn = sub.add_parser("read_notes", aliases=["notes"])
    rn.add_argument("scope")
    rn.add_argument("--limit", type=int, default=50)

    ho = sub.add_parser("handoff", parents=[sess])
    ho.add_argument("paths", nargs="+")
    ho.add_argument("--summary", required=True)
    ho.add_argument("--status", required=True)

    # ---- layer 2: gawd docs ----
    cgd = sub.add_parser("create_gawd_doc")
    cgd.add_argument("goal")
    cgd.add_argument("--saga-id")
    cgd.add_argument("--constraints", nargs="*", default=[])
    cgd.add_argument("--success-criteria", nargs="*", default=[])
    cgd.add_argument("--unresolved", nargs="*", default=[])
    cgd.add_argument("--acceptance-criteria", nargs="*", default=[])
    cgd.add_argument("--task-graph-json")

    agd = sub.add_parser("approve_gawd_doc")
    agd.add_argument("gawd_doc_id")

    ggd = sub.add_parser("get_gawd_doc")
    ggd.add_argument("gawd_doc_id")

    asgd = sub.add_parser("attach_gawd_doc_to_saga")
    asgd.add_argument("saga_id")
    asgd.add_argument("gawd_doc_id")

    ckamb = sub.add_parser("check_ambiguity")
    ckamb.add_argument("gawd_doc_id")

    # ---- layer 2: sagas ----
    cs = sub.add_parser("create_saga")
    cs.add_argument("goal")
    cs.add_argument("--budget-tokens", type=int, default=1_000_000)
    cs.add_argument("--budget-seconds", type=int, default=86400)
    cs.add_argument("--gawd-doc-id")
    # sha256 of the draft this saga came from. Passing it makes a repeated
    # ingest replay onto the existing saga instead of creating a second one.
    cs.add_argument("--content-digest")

    gs = sub.add_parser("get_saga")
    gs.add_argument("saga_id")

    ls = sub.add_parser("list_sagas")
    ls.add_argument("--status")

    cks = sub.add_parser("check_stagnation")
    cks.add_argument("saga_id")

    # Layer 3 — DesignDoc-governed WorkUnits
    cdd = sub.add_parser("compile_design_doc")
    cdd.add_argument("path_or_revision")
    cdd.add_argument("--design-doc-id")
    cdd.add_argument("--classify-phases", action="store_true")

    swu = sub.add_parser("start_work_unit")
    swu.add_argument("compiled_plan_revision_id")
    swu.add_argument("--title")
    swu.add_argument("--approved-plan-hash")

    gwu = sub.add_parser("get_work_unit")
    gwu.add_argument("work_unit_id")

    lwu = sub.add_parser("list_work_units")
    lwu.add_argument("--status")

    sub.add_parser("list_design_docs")

    lwe = sub.add_parser("list_work_unit_events")
    lwe.add_argument("work_unit_id")
    lwe.add_argument("--after-sequence", type=int, default=0)
    lwe.add_argument("--limit", type=int, default=100)

    lwa = sub.add_parser("list_work_unit_artifacts")
    lwa.add_argument("work_unit_id")

    swd = sub.add_parser("submit_work_unit_decision")
    swd.add_argument("work_unit_id")
    swd.add_argument("request_id")
    swd.add_argument("decision", choices=["APPROVED", "DENIED", "ANSWERED"])
    swd.add_argument("idempotency_key")
    swd.add_argument("--decided-by", default="operator")

    cwu = sub.add_parser("cancel_work_unit")
    cwu.add_argument("work_unit_id")
    cwu.add_argument("--reason", default="cancelled by operator")

    rwu = sub.add_parser("resume_work_unit")
    rwu.add_argument("work_unit_id")
    rwu.add_argument("--inline", action="store_true")

    arwd = sub.add_parser("adopt_recovered_work_unit_dispatch")
    arwd.add_argument("intent_id")

    aswd = sub.add_parser("adopt_settled_work_unit_dispatch")
    aswd.add_argument("work_unit_id")
    aswd.add_argument("milestone_key")

    aiwm = sub.add_parser("adopt_integrated_work_unit_milestone")
    aiwm.add_argument("work_unit_id")
    aiwm.add_argument("milestone_key")
    aiwm.add_argument("commit_sha")
    aiwm.add_argument("--accepted-by", required=True)
    aiwm.add_argument("--acceptance-evidence", required=True)

    dwe = sub.add_parser("drain_work_unit_enqueues")
    dwe.add_argument("--limit", type=int, default=20)
    dwe.add_argument("--inline", action="store_true")

    rcr = sub.add_parser("run_crash_reconciler")
    rcr.add_argument("--interval-seconds", type=float, default=30.0)
    rcr.add_argument("--max-polls", type=int, default=None)
    rcr.add_argument("--max-automatic-recoveries", type=int, default=3)

    red = sub.add_parser("run_enqueue_drainer")
    red.add_argument("--interval-seconds", type=float, default=5.0)
    red.add_argument("--limit", type=int, default=20)
    red.add_argument("--max-polls", type=int, default=None)
    red.add_argument("--inline", action="store_true")
    red.add_argument("--max-transient-resumes", type=int, default=3)

    rel = sub.add_parser("read_execution_ledger")
    rel.add_argument("--workflow-name", default=None)

    rld = sub.add_parser("run_ledger_dispatcher")
    rld.add_argument("--interval-seconds", type=float, default=2.0)
    rld.add_argument("--max-polls", type=int, default=None)
    rld.add_argument("--tier", default=None)
    rld.add_argument("--dispatcher-name", default="dispatcher")

    sub.add_parser("describe_resident_loops")

    # The only command in this program that may reshape the ledger's schema.
    # Everything else refuses, including connecting, which is the point: a
    # migration is a decision about a database several processes share, so it
    # gets typed on purpose rather than performed on the way past.
    sub.add_parser(
        "migrate_coordination_schema",
        help="apply pending coordination DDL to the ledger this process points at",
    )

    # ---- layer 2: saga milestones ----
    csm = sub.add_parser("create_saga_milestone")
    csm.add_argument("saga_id")
    csm.add_argument("name")
    csm.add_argument("--sequence", type=int, required=True)
    csm.add_argument("--milestone-id")
    csm.add_argument("--gawd-doc-id")
    csm.add_argument("--description", default="")
    csm.add_argument("--depends-on", action="append", default=[])
    csm.add_argument("--entry-criteria", action="append", default=[])
    csm.add_argument("--exit-criteria", action="append", default=[])
    csm.add_argument("--required-artifact", action="append", default=[])
    csm.add_argument("--approval-required", action="store_true")

    asm = sub.add_parser("amend_saga_milestone")
    asm.add_argument("milestone_id")
    asm.add_argument("--description")
    asm.add_argument("--entry-criteria", action="append")
    asm.add_argument("--exit-criteria", action="append")
    asm.add_argument("--required-artifact", action="append")
    asm.add_argument("--reason", required=True)
    asm.add_argument("--amended-by", default="operator")

    lsm = sub.add_parser("list_saga_milestones")
    lsm.add_argument("saga_id")
    lsm.add_argument("--status")

    gsm = sub.add_parser("get_saga_milestone")
    gsm.add_argument("milestone_id")

    rme = sub.add_parser("record_milestone_evidence")
    rme.add_argument("milestone_id")
    rme.add_argument("evidence_type", choices=sorted(MILESTONE_EVIDENCE_TYPES))
    rme.add_argument("content")
    rme.add_argument("--schema-version", default="milestone_evidence.v1")

    ssm = sub.add_parser("start_saga_milestone")
    ssm.add_argument("milestone_id")
    ssm.add_argument("--dispatch-intent-id")

    cos = sub.add_parser("complete_saga_milestone")
    cos.add_argument("milestone_id")
    cos.add_argument("--evidence-type", choices=sorted(MILESTONE_EVIDENCE_TYPES))
    cos.add_argument("--evidence-content")
    cos.add_argument(
        "--outcome",
        choices=[
            TerminalOutcome.AUTOMATED_COMPLETION.value,
            TerminalOutcome.MANUAL_RECOVERY_COMPLETION.value,
        ],
        default=TerminalOutcome.MANUAL_RECOVERY_COMPLETION.value,
    )

    fsm = sub.add_parser("fail_saga_milestone")
    fsm.add_argument("milestone_id")
    fsm.add_argument("reason")
    fsm.add_argument("--status", choices=["FAILED", "BLOCKED", "CANCELED"], default="FAILED")

    rsmilestone = sub.add_parser("retry_saga_milestone")
    rsmilestone.add_argument("milestone_id")
    rsmilestone.add_argument("reason")

    nrsm = sub.add_parser("next_ready_saga_milestone")
    nrsm.add_argument("saga_id")

    rsm = sub.add_parser("reconcile_saga_milestones")
    rsm.add_argument("saga_id")

    # ---- layer 2: pow-wows ----
    cpw = sub.add_parser("create_pow_wow")
    cpw.add_argument("saga_id")
    cpw.add_argument("stage")
    cpw.add_argument("goal")
    cpw.add_argument("--exit-criteria", default="")
    cpw.add_argument("--budget-tokens", type=int, default=100_000)
    cpw.add_argument("--allowed-tools", nargs="*", default=[])
    cpw.add_argument("--required-outputs", nargs="*", default=[])

    gpw = sub.add_parser("get_pow_wow")
    gpw.add_argument("pow_wow_id")

    lpw = sub.add_parser("list_pow_wows")
    lpw.add_argument("saga_id")
    lpw.add_argument("--status")

    compw = sub.add_parser("complete_pow_wow", parents=[sess])
    compw.add_argument("pow_wow_id")
    compw.add_argument("output_summary")
    compw.add_argument("--status", default="COMPLETED")

    # ---- layer 2: tasks ----
    ct = sub.add_parser("claim_task", parents=[sess])
    ct.add_argument("pow_wow_id")
    ct.add_argument("task_name")
    ct.add_argument("description")
    ct.add_argument("--blocked-by", action="append", default=[])

    ctask = sub.add_parser("complete_task", parents=[sess])
    ctask.add_argument("task_id")

    ftask = sub.add_parser("fail_task", parents=[sess])
    ftask.add_argument("task_id")
    ftask.add_argument("reason")

    lt = sub.add_parser("list_tasks")
    lt.add_argument("pow_wow_id")
    lt.add_argument("--status")

    dt = sub.add_parser("delegate_task", parents=[sess])
    dt.add_argument("prompt")
    dt.add_argument("--tier", choices=["weak", "strong", "special"], default="weak")
    dt.add_argument("--adapter", default="local_llama")
    dt.add_argument("--model-role", default="general")
    dt.add_argument("--role", default="delegate")
    dt.add_argument("--pow-wow-id")
    dt.add_argument("--task-id")
    dt.add_argument("--max-tokens", type=int, default=2048)
    dt.add_argument("--timeout-seconds", type=int, default=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS)
    dt.add_argument("--no-submit-result", action="store_true")

    # ---- layer 2: artifacts ----
    sa = sub.add_parser("submit_artifact", parents=[sess])
    sa.add_argument("pow_wow_id")
    sa.add_argument("artifact_type")
    sa.add_argument("content", nargs="?")
    sa.add_argument("--content-file", type=Path)
    sa.add_argument("--task-id")
    sa.add_argument("--schema-version", default="v1")

    # ---- layer 2: approvals ----
    sar = sub.add_parser("submit_approval_request")
    sar.add_argument("saga_id")
    sar.add_argument(
        "request_type",
        choices=list(APPROVAL_REQUEST_TYPES),
    )
    sar.add_argument("--requested-by")
    sar.add_argument("--payload", help="JSON object with request context")
    sar.add_argument("--payload-file", type=Path)

    rar = sub.add_parser("resolve_approval_request")
    rar.add_argument("approval_id")
    rar.add_argument("decision", choices=["approve", "deny"])
    rar.add_argument("--resolved-by", required=True)

    rvar = sub.add_parser("revoke_approval_request")
    rvar.add_argument("approval_id")
    rvar.add_argument("--revoked-by", required=True)
    rvar.add_argument("--reason", required=True)

    lar = sub.add_parser("list_approval_requests")
    lar.add_argument("--saga-id")
    lar.add_argument("--status")

    # ---- layer 2: tool permissions ----
    # Only the two operator verbs. Request, grant, deny, and list are agent and
    # MCP surfaces; revoke and restore are the operator's change of mind and
    # its lifting, and an operator at a terminal must be able to type both.
    rtp = sub.add_parser("revoke_tool_permission")
    rtp.add_argument("pow_wow_id")
    rtp.add_argument("agent_name")
    rtp.add_argument("tool_name")
    rtp.add_argument("--revoked-by", required=True)

    rstp = sub.add_parser("restore_tool_permission")
    rstp.add_argument("pow_wow_id")
    rstp.add_argument("agent_name")
    rstp.add_argument("tool_name")
    rstp.add_argument("--restored-by", required=True)
    rstp.add_argument("--reason", required=True)

    rusr = sub.add_parser("recover_unparsed_staff_review")
    rusr.add_argument("intent_id")

    lir = sub.add_parser("list_integration_requests")
    lir.add_argument("--target-project-id")
    lir.add_argument("--state")

    rrf = sub.add_parser("run_refinery")
    rrf.add_argument("target_project_id")
    rrf.add_argument("--interval-seconds", type=float, default=None)
    rrf.add_argument("--max-polls", type=int, default=None)

    rrff = sub.add_parser("run_refinery_fleet")
    rrff.add_argument("--target-project-id", action="append", required=True)
    rrff.add_argument("--interval-seconds", type=float, default=None)
    rrff.add_argument("--max-polls", type=int, default=None)

    # ---- layer 3: dispatch intents ----
    sdi = sub.add_parser("submit_dispatch_intent")
    sdi.add_argument("tier", choices=["junior", "senior", "staff"])
    sdi.add_argument("prompt")
    dispatch_kind_choices = tuple(item.value for item in DispatchKind)
    sdi.add_argument(
        "--kind",
        choices=dispatch_kind_choices,
        default=DispatchKind.ADVISORY.value,
    )
    sdi.add_argument("--target-project-id")
    sdi.add_argument("--source")
    sdi.add_argument("--fanout", type=int, default=1)
    sdi.add_argument(
        "--allow-tier",
        action="append",
        dest="allow_tiers",
        choices=["junior", "senior", "staff"],
        help="Tier eligible to answer; repeat for a quorum or overflow list.",
    )
    sdi.add_argument("--reduce", choices=["none", "vote", "judge"], default="none")
    sdi.add_argument("--reducer-tier", choices=["junior", "senior", "staff"])
    sdi.add_argument("--permitted-capability", action="append", default=[])

    cndi = sub.add_parser("claim_next_dispatch_intent")
    cndi.add_argument("--claimed-by", required=True)
    cndi.add_argument("--tier", choices=["junior", "senior", "staff"])

    cdi = sub.add_parser("complete_dispatch_intent")
    cdi.add_argument("intent_id")
    cdi.add_argument("status", choices=["DONE", "FAILED"])
    cdi.add_argument("--result")
    cdi.add_argument("--result-file", type=Path)
    cdi.add_argument("--error")

    xdi = sub.add_parser("cancel_dispatch_intent")
    xdi.add_argument("intent_id")
    xdi.add_argument("--reason")
    xdi.add_argument("--canceled-by", default="operator")

    sudi = sub.add_parser("supersede_dispatch_intent")
    sudi.add_argument("old_intent_id")
    sudi.add_argument("--prompt")
    sudi.add_argument("--tier", choices=["junior", "senior", "staff"])
    sudi.add_argument("--kind", choices=dispatch_kind_choices)
    sudi.add_argument("--target-project-id")
    sudi.add_argument("--source")
    sudi.add_argument("--reason")
    sudi.add_argument("--superseded-by", default="operator")

    ga = sub.add_parser("get_artifact")
    ga.add_argument("artifact_id")

    lra = sub.add_parser("latest_repo_audit")
    lra.add_argument("target_project_id")
    lra.add_argument("tier")

    ldi = sub.add_parser("list_dispatch_intents")
    ldi.add_argument("--status")
    ldi.add_argument("--parent-intent-id")

    sub.add_parser("list_doctrine_stale_reviews")

    oel = sub.add_parser("open_execution_lease")
    oel.add_argument("idempotency_key")
    oel.add_argument("--worker-id", required=True)
    oel.add_argument("--intent-id")
    oel.add_argument("--task-id")
    oel.add_argument("--agent-tier")
    oel.add_argument("--agent-name")
    oel.add_argument("--task-role")
    oel.add_argument("--model")
    oel.add_argument("--target-project-id")
    oel.add_argument("--planning-phase")
    oel.add_argument("--source-revision")
    oel.add_argument("--permission-envelope-sha256")
    oel.add_argument("--resumed-thread-id")
    oel.add_argument("--worktree-path")
    oel.add_argument("--command-json")
    oel.add_argument("--compensation-json")
    oel.add_argument("--timeout-seconds", type=int, default=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS)

    hel = sub.add_parser("heartbeat_execution_lease")
    hel.add_argument("lease_id")
    hel.add_argument("--worker-id", required=True)

    xel = sub.add_parser("request_execution_cancel")
    xel.add_argument("lease_id")
    xel.add_argument("--reason")
    xel.add_argument("--requested-by", default="operator")

    cel = sub.add_parser("complete_execution_lease")
    cel.add_argument("lease_id")
    cel.add_argument(
        "status",
        choices=sorted(LEASE_TERMINAL_STATUSES),
    )
    cel.add_argument("--result-json")
    cel.add_argument("--error")

    lel = sub.add_parser("list_execution_leases")
    lel.add_argument("--status")

    aee = sub.add_parser("append_execution_event")
    aee.add_argument("lease_id")
    aee.add_argument("--sequence", type=int, required=True)
    aee.add_argument("--occurred-at", type=float, required=True)
    aee.add_argument("--source", choices=["stdout", "stderr", "lifecycle"], required=True)
    aee.add_argument("--kind", required=True)
    aee.add_argument("--payload", required=True)
    aee.add_argument("--payload-sha256", required=True)

    lee = sub.add_parser("list_execution_events")
    lee.add_argument("lease_id")
    lee.add_argument("--after-sequence", type=int, default=0)
    lee.add_argument("--limit", type=int, default=200)

    fac = sub.add_parser("find_agent_continuation")
    fac.add_argument("source_task_id")
    fac.add_argument("--pow-wow-id", required=True)
    fac.add_argument("--agent-name", required=True)
    fac.add_argument("--model")
    fac.add_argument("--target-project-id", required=True)
    fac.add_argument("--source-revision", required=True)

    lfur = sub.add_parser("list_frontier_usage_records")
    lfur.add_argument("lease_id")

    aea = sub.add_parser("attach_execution_artifact")
    aea.add_argument("lease_id")
    aea.add_argument("artifact_id")
    aea.add_argument("--role", required=True)
    aea.add_argument("--schema-version", required=True)

    lea = sub.add_parser("list_execution_artifacts")
    lea.add_argument("lease_id")

    cec = sub.add_parser("create_execution_checkpoint")
    cec.add_argument("lease_id")
    cec.add_argument(
        "--reason",
        choices=["deadline", "operator_cancel", "supervisor_error"],
        required=True,
    )
    cec.add_argument(
        "--status",
        choices=["PENDING_JUNIOR", "DECIDED", "PAUSED", "FAILED"],
        required=True,
    )
    cec.add_argument("--saga-id")
    cec.add_argument("--pow-wow-id")
    cec.add_argument("--worktree-path")
    cec.add_argument("--source-repo-path")
    cec.add_argument("--base-head-sha")
    cec.add_argument("--transcript-artifact-id")
    cec.add_argument("--patch-artifact-id")
    cec.add_argument("--git-status-artifact-id")
    cec.add_argument("--test-summary-artifact-id")
    cec.add_argument("--task-contract", default="")
    cec.add_argument("--event-summary", default="")
    cec.add_argument("--submit-review", action="store_true")
    cec.add_argument("--error")

    gec = sub.add_parser("get_execution_checkpoint")
    gec.add_argument("checkpoint_id")

    lec = sub.add_parser("list_execution_checkpoints")
    lec.add_argument("--status")

    dec = sub.add_parser("decide_execution_checkpoint")
    dec.add_argument("checkpoint_id")
    dec.add_argument("--decision-json", required=True)
    dec.add_argument("--junior-review-artifact-id")

    rrsr = sub.add_parser("request_recovery_staff_review")
    rrsr.add_argument("checkpoint_id")
    rrsr.add_argument("--target-project-id", required=True)
    rrsr.add_argument("--branch", required=True)
    rrsr.add_argument("--base-head-sha", required=True)
    rrsr.add_argument("--commit-sha", required=True)
    rrsr.add_argument("--milestone-id")

    cle = sub.add_parser("claim_next_ledger_event")
    cle.add_argument("--claimed-by", required=True)
    cle.add_argument("--event-type")

    cele = sub.add_parser("complete_ledger_event")
    cele.add_argument("event_id")
    cele.add_argument("status", choices=["PROCESSED", "FAILED"])
    cele.add_argument("--error")

    lle = sub.add_parser("list_ledger_events")
    lle.add_argument("--status")

    gc = sub.add_parser("gc")
    gc.add_argument("--retention-seconds", type=int)
    gc.add_argument(
        "--abandoned-after-seconds",
        type=int,
        default=WORK_ABANDONED_AFTER_SECONDS,
    )

    return p


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """Run one parsed command against the ledger this process is pointed at.

    Selecting the ledger is the caller's job, because the two callers select it
    differently: `main` from `--root` and the inherited environment, the
    in-process transport from a `CoordinationLedgerSelection`.
    """

    cmd = args.cmd

    # layer 1
    if cmd in ("register_agent", "register-agent", "register"):
        out = register_agent(args.agent_name, args.session)
    elif cmd == "heartbeat":
        out = heartbeat(args.session)
    elif cmd in ("list_sessions", "sessions"):
        out = list_sessions()
    elif cmd in ("append_note", "note"):
        out = append_note(args.scope, args.message, args.session)
    elif cmd in ("read_notes", "notes"):
        out = read_notes(args.scope, args.limit)
    elif cmd == "handoff":
        out = handoff(args.paths, args.summary, args.status, args.session)

    # layer 2 — gawd docs
    elif cmd == "create_gawd_doc":
        out = create_gawd_doc(
            goal=args.goal,
            constraints=args.constraints,
            success_criteria=args.success_criteria,
            unresolved_questions=args.unresolved,
            acceptance_criteria=args.acceptance_criteria,
            task_graph=json.loads(args.task_graph_json) if args.task_graph_json else None,
            saga_id=args.saga_id,
        )
    elif cmd == "approve_gawd_doc":
        out = approve_gawd_doc(args.gawd_doc_id)
    elif cmd == "get_gawd_doc":
        out = get_gawd_doc(args.gawd_doc_id)
    elif cmd == "attach_gawd_doc_to_saga":
        out = attach_gawd_doc_to_saga(args.saga_id, args.gawd_doc_id)
    elif cmd == "check_ambiguity":
        out = check_ambiguity(args.gawd_doc_id)

    # layer 2 — sagas
    elif cmd == "create_saga":
        out = create_saga(
            goal=args.goal,
            budget_tokens=args.budget_tokens,
            budget_seconds=args.budget_seconds,
            gawd_doc_id=args.gawd_doc_id,
            content_digest=args.content_digest,
        )
    elif cmd == "get_saga":
        out = get_saga(args.saga_id)
    elif cmd == "list_sagas":
        out = list_sagas(status_filter=args.status)
    elif cmd == "check_stagnation":
        out = check_stagnation(args.saga_id)

    # layer 3 — DesignDoc-governed WorkUnits
    elif cmd == "compile_design_doc":
        out = work_unit_commands.compile_design_doc(
            args.path_or_revision,
            design_doc_id=args.design_doc_id,
            classify_phases=args.classify_phases,
        )
    elif cmd == "start_work_unit":
        out = work_unit_commands.start_work_unit(
            args.compiled_plan_revision_id,
            title=args.title,
            approved_plan_hash=args.approved_plan_hash,
        )
    elif cmd == "get_work_unit":
        out = work_unit_commands.get_work_unit(args.work_unit_id)
    elif cmd == "list_work_units":
        out = work_unit_commands.list_work_units(args.status)
    elif cmd == "list_design_docs":
        out = work_unit_commands.list_design_docs()
    elif cmd == "list_work_unit_events":
        out = work_unit_commands.list_work_unit_events(
            args.work_unit_id,
            after_sequence=args.after_sequence,
            limit=args.limit,
        )
    elif cmd == "list_work_unit_artifacts":
        out = work_unit_commands.list_work_unit_artifacts(args.work_unit_id)
    elif cmd == "submit_work_unit_decision":
        out = work_unit_commands.submit_work_unit_decision(
            args.work_unit_id,
            args.request_id,
            args.decision,
            args.idempotency_key,
            decided_by=args.decided_by,
        )
    elif cmd == "cancel_work_unit":
        out = work_unit_commands.cancel_work_unit(args.work_unit_id, reason=args.reason)
    elif cmd == "resume_work_unit":
        out = work_unit_commands.resume_work_unit(args.work_unit_id, inline=args.inline)
    elif cmd == "adopt_recovered_work_unit_dispatch":
        out = work_unit_commands.adopt_recovered_work_unit_dispatch(args.intent_id)
    elif cmd == "adopt_settled_work_unit_dispatch":
        out = work_unit_commands.adopt_settled_work_unit_dispatch(
            args.work_unit_id,
            args.milestone_key,
        )
    elif cmd == "adopt_integrated_work_unit_milestone":
        out = work_unit_commands.adopt_integrated_work_unit_milestone(
            args.work_unit_id,
            args.milestone_key,
            args.commit_sha,
            args.accepted_by,
            args.acceptance_evidence,
        )
    elif cmd == "drain_work_unit_enqueues":
        out = work_unit_commands.drain_work_unit_enqueues(args.limit, inline=args.inline)
    elif cmd == "run_crash_reconciler":
        out = work_unit_commands.run_crash_reconciler(
            args.interval_seconds,
            args.max_polls,
            args.max_automatic_recoveries,
        )
    elif cmd == "run_enqueue_drainer":
        out = work_unit_commands.run_enqueue_drainer(
            args.interval_seconds,
            args.limit,
            args.max_polls,
            inline=args.inline,
            max_transient_resumes=args.max_transient_resumes,
        )
    elif cmd == "read_execution_ledger":
        out = read_execution_ledger(args.workflow_name)
    elif cmd == "run_ledger_dispatcher":
        out = run_ledger_dispatcher(
            args.interval_seconds,
            args.max_polls,
            args.tier,
            args.dispatcher_name,
        )
    elif cmd == "describe_resident_loops":
        out = describe_resident_loops()
    elif cmd == "migrate_coordination_schema":
        out = ok(**migrate_postgres_schema())

    # layer 2 — saga milestones
    elif cmd == "create_saga_milestone":
        out = create_saga_milestone(
            saga_id=args.saga_id,
            name=args.name,
            sequence=args.sequence,
            milestone_id=args.milestone_id,
            gawd_doc_id=args.gawd_doc_id,
            description=args.description,
            depends_on=args.depends_on,
            entry_criteria=args.entry_criteria,
            exit_criteria=args.exit_criteria,
            required_artifacts=args.required_artifact,
            approval_required=args.approval_required,
        )
    elif cmd == "amend_saga_milestone":
        out = amend_saga_milestone(
            args.milestone_id,
            reason=args.reason,
            amended_by=args.amended_by,
            description=args.description,
            entry_criteria=args.entry_criteria,
            exit_criteria=args.exit_criteria,
            required_artifacts=args.required_artifact,
        )
    elif cmd == "list_saga_milestones":
        out = list_saga_milestones(args.saga_id, status_filter=args.status)
    elif cmd == "get_saga_milestone":
        out = get_saga_milestone(args.milestone_id)
    elif cmd == "record_milestone_evidence":
        out = record_milestone_evidence(
            args.milestone_id,
            args.evidence_type,
            args.content,
            schema_version=args.schema_version,
        )
    elif cmd == "start_saga_milestone":
        out = start_saga_milestone(
            args.milestone_id,
            dispatch_intent_id=args.dispatch_intent_id,
        )
    elif cmd == "complete_saga_milestone":
        out = complete_saga_milestone(
            args.milestone_id,
            evidence_type=args.evidence_type,
            evidence_content=args.evidence_content,
            outcome=args.outcome,
        )
    elif cmd == "fail_saga_milestone":
        out = fail_saga_milestone(
            args.milestone_id,
            args.reason,
            status=args.status,
        )
    elif cmd == "retry_saga_milestone":
        out = retry_saga_milestone(args.milestone_id, args.reason)
    elif cmd == "next_ready_saga_milestone":
        out = next_ready_saga_milestone(args.saga_id)
    elif cmd == "reconcile_saga_milestones":
        out = reconcile_saga_milestones(args.saga_id)

    # layer 2 — pow-wows
    elif cmd == "create_pow_wow":
        out = create_pow_wow(
            saga_id=args.saga_id,
            stage=args.stage,
            goal=args.goal,
            exit_criteria=args.exit_criteria,
            budget_tokens=args.budget_tokens,
            allowed_tools=args.allowed_tools,
            required_outputs=args.required_outputs,
        )
    elif cmd == "get_pow_wow":
        out = get_pow_wow(args.pow_wow_id)
    elif cmd == "list_pow_wows":
        out = list_pow_wows(saga_id=args.saga_id, status_filter=args.status)
    elif cmd == "complete_pow_wow":
        out = complete_pow_wow(
            pow_wow_id=args.pow_wow_id,
            output_summary=args.output_summary,
            status=args.status,
            session_id=args.session,
        )

    # layer 2 — tasks
    elif cmd == "claim_task":
        out = claim_task(
            args.pow_wow_id,
            args.task_name,
            args.description,
            blocked_by=args.blocked_by,
            session_id=args.session,
        )
    elif cmd == "complete_task":
        out = complete_task(args.task_id, session_id=args.session)
    elif cmd == "fail_task":
        out = fail_task(args.task_id, args.reason, session_id=args.session)
    elif cmd == "list_tasks":
        out = list_tasks(args.pow_wow_id, status_filter=args.status)
    elif cmd == "delegate_task":
        out = delegate_task(
            prompt=args.prompt,
            tier=args.tier,
            adapter=args.adapter,
            model_role=args.model_role,
            role=args.role,
            pow_wow_id=args.pow_wow_id,
            task_id=args.task_id,
            max_tokens=args.max_tokens,
            timeout_seconds=args.timeout_seconds,
            submit_result=not args.no_submit_result,
            session_id=args.session,
        )

    # layer 2 — artifacts
    elif cmd == "submit_artifact":
        if args.content is not None and args.content_file is not None:
            raise ValueError("submit_artifact accepts either content or --content-file, not both")
        if args.content_file is not None:
            content = args.content_file.read_text(encoding="utf-8")
        elif args.content is not None:
            content = args.content
        else:
            raise ValueError("submit_artifact requires content or --content-file")
        out = submit_artifact(
            pow_wow_id=args.pow_wow_id,
            artifact_type=args.artifact_type,
            content=content,
            task_id=args.task_id,
            schema_version=args.schema_version,
            session_id=args.session,
        )

    # layer 2 — approvals
    elif cmd == "submit_approval_request":
        if args.payload is not None and args.payload_file is not None:
            raise ValueError(
                "submit_approval_request accepts either --payload or --payload-file, not both"
            )
        approval_payload = (
            args.payload_file.read_text(encoding="utf-8")
            if args.payload_file is not None
            else args.payload
        )
        out = submit_approval_request(
            saga_id=args.saga_id,
            request_type=args.request_type,
            payload=json.loads(approval_payload) if approval_payload else None,
            requested_by=args.requested_by,
        )
    elif cmd == "resolve_approval_request":
        out = resolve_approval_request(
            approval_id=args.approval_id,
            approved=args.decision == "approve",
            resolved_by=args.resolved_by,
        )
    elif cmd == "revoke_approval_request":
        out = revoke_approval_request(
            approval_id=args.approval_id,
            revoked_by=args.revoked_by,
            reason=args.reason,
        )
    elif cmd == "list_approval_requests":
        out = list_approval_requests(saga_id=args.saga_id, status_filter=args.status)

    # layer 2 — tool permissions (operator verbs)
    elif cmd == "revoke_tool_permission":
        out = revoke_tool_permission(
            pow_wow_id=args.pow_wow_id,
            agent_name=args.agent_name,
            tool_name=args.tool_name,
            revoked_by=args.revoked_by,
        )
    elif cmd == "restore_tool_permission":
        out = restore_tool_permission(
            pow_wow_id=args.pow_wow_id,
            agent_name=args.agent_name,
            tool_name=args.tool_name,
            restored_by=args.restored_by,
            reason=args.reason,
        )
    elif cmd == "recover_unparsed_staff_review":
        out = recover_unparsed_staff_review(args.intent_id)
    elif cmd == "list_integration_requests":
        out = list_integration_requests(
            target_project_id=args.target_project_id,
            state=args.state,
        )
    elif cmd == "run_refinery":
        out = run_refinery(
            args.target_project_id,
            args.interval_seconds,
            args.max_polls,
        )
    elif cmd == "run_refinery_fleet":
        out = run_refinery_fleet(
            args.target_project_id,
            args.interval_seconds,
            args.max_polls,
        )

    # layer 3 — dispatch intents
    elif cmd == "submit_dispatch_intent":
        out = submit_dispatch_intent(
            tier=args.tier,
            prompt=args.prompt,
            kind=args.kind,
            target_project_id=args.target_project_id,
            source=args.source,
            fanout=args.fanout,
            allow_tiers=args.allow_tiers,
            reduce=args.reduce,
            reducer_tier=args.reducer_tier,
            permitted_capabilities=args.permitted_capability,
        )
    elif cmd == "claim_next_dispatch_intent":
        out = claim_next_dispatch_intent(claimed_by=args.claimed_by, tier=args.tier)
    elif cmd == "complete_dispatch_intent":
        if args.result is not None and args.result_file is not None:
            raise ValueError(
                "complete_dispatch_intent accepts either --result or --result-file, not both"
            )
        result = (
            args.result_file.read_text(encoding="utf-8")
            if args.result_file is not None
            else args.result
        )
        out = complete_dispatch_intent(
            intent_id=args.intent_id,
            status=args.status,
            result=result,
            error=args.error,
        )
    elif cmd == "cancel_dispatch_intent":
        out = cancel_dispatch_intent(
            intent_id=args.intent_id,
            reason=args.reason,
            canceled_by=args.canceled_by,
        )
    elif cmd == "supersede_dispatch_intent":
        out = supersede_dispatch_intent(
            old_intent_id=args.old_intent_id,
            prompt=args.prompt,
            tier=args.tier,
            kind=args.kind,
            target_project_id=args.target_project_id,
            source=args.source,
            reason=args.reason,
            superseded_by=args.superseded_by,
        )
    elif cmd == "get_artifact":
        out = get_artifact(args.artifact_id)
    elif cmd == "latest_repo_audit":
        out = latest_repo_audit(args.target_project_id, args.tier)
    elif cmd == "list_dispatch_intents":
        out = list_dispatch_intents(
            status_filter=args.status,
            parent_intent_id=args.parent_intent_id,
        )
    elif cmd == "list_doctrine_stale_reviews":
        out = list_doctrine_stale_reviews()
    elif cmd == "open_execution_lease":
        out = open_execution_lease(
            idempotency_key=args.idempotency_key,
            worker_id=args.worker_id,
            intent_id=args.intent_id,
            task_id=args.task_id,
            agent_tier=args.agent_tier,
            agent_name=args.agent_name,
            task_role=args.task_role,
            model=args.model,
            target_project_id=args.target_project_id,
            planning_phase=args.planning_phase,
            source_revision=args.source_revision,
            permission_envelope_sha256=args.permission_envelope_sha256,
            resumed_thread_id=args.resumed_thread_id,
            worktree_path=args.worktree_path,
            command_json=args.command_json,
            compensation_json=args.compensation_json,
            timeout_seconds=args.timeout_seconds,
        )
    elif cmd == "heartbeat_execution_lease":
        out = heartbeat_execution_lease(
            lease_id=args.lease_id,
            worker_id=args.worker_id,
        )
    elif cmd == "request_execution_cancel":
        out = request_execution_cancel(
            lease_id=args.lease_id,
            reason=args.reason,
            requested_by=args.requested_by,
        )
    elif cmd == "complete_execution_lease":
        out = complete_execution_lease(
            lease_id=args.lease_id,
            status=args.status,
            result_json=args.result_json,
            error=args.error,
        )
    elif cmd == "list_execution_leases":
        out = list_execution_leases(status_filter=args.status)
    elif cmd == "append_execution_event":
        out = append_execution_event(
            lease_id=args.lease_id,
            sequence=args.sequence,
            occurred_at=args.occurred_at,
            source=args.source,
            kind=args.kind,
            payload=json.loads(args.payload),
            payload_sha256=args.payload_sha256,
        )
    elif cmd == "list_execution_events":
        out = list_execution_events(
            args.lease_id,
            after_sequence=args.after_sequence,
            limit=args.limit,
        )
    elif cmd == "find_agent_continuation":
        out = find_compatible_agent_continuation(
            args.source_task_id,
            pow_wow_id=args.pow_wow_id,
            harness=args.agent_name,
            source_model=args.model,
            target_project_id=args.target_project_id,
            source_revision=args.source_revision,
        )
    elif cmd == "list_frontier_usage_records":
        out = list_frontier_usage_records(args.lease_id)
    elif cmd == "attach_execution_artifact":
        out = attach_execution_artifact(
            args.lease_id,
            args.artifact_id,
            args.role,
            args.schema_version,
        )
    elif cmd == "list_execution_artifacts":
        out = list_execution_artifacts(args.lease_id)
    elif cmd == "create_execution_checkpoint":
        out = create_execution_checkpoint(
            args.lease_id,
            reason=args.reason,
            status=args.status,
            saga_id=args.saga_id,
            pow_wow_id=args.pow_wow_id,
            worktree_path=args.worktree_path,
            source_repo_path=args.source_repo_path,
            base_head_sha=args.base_head_sha,
            transcript_artifact_id=args.transcript_artifact_id,
            patch_artifact_id=args.patch_artifact_id,
            git_status_artifact_id=args.git_status_artifact_id,
            test_summary_artifact_id=args.test_summary_artifact_id,
            task_contract=args.task_contract,
            event_summary=args.event_summary,
            submit_review=args.submit_review,
            error=args.error,
        )
    elif cmd == "get_execution_checkpoint":
        out = get_execution_checkpoint(args.checkpoint_id)
    elif cmd == "list_execution_checkpoints":
        out = list_execution_checkpoints(status_filter=args.status)
    elif cmd == "decide_execution_checkpoint":
        out = decide_execution_checkpoint(
            args.checkpoint_id,
            json.loads(args.decision_json),
            junior_review_artifact_id=args.junior_review_artifact_id,
        )
    elif cmd == "request_recovery_staff_review":
        out = request_recovery_staff_review(
            args.checkpoint_id,
            target_project_id=args.target_project_id,
            branch=args.branch,
            base_sha=args.base_head_sha,
            commit_sha=args.commit_sha,
            milestone_id=args.milestone_id,
        )
    elif cmd == "claim_next_ledger_event":
        out = claim_next_ledger_event(
            claimed_by=args.claimed_by,
            event_type=args.event_type,
        )
    elif cmd == "complete_ledger_event":
        out = complete_ledger_event(
            event_id=args.event_id,
            status=args.status,
            error=args.error,
        )
    elif cmd == "list_ledger_events":
        out = list_ledger_events(status_filter=args.status)
    elif cmd == "gc":
        out = gc_ledger(
            retention_seconds=args.retention_seconds,
            abandoned_after_seconds=args.abandoned_after_seconds,
        )

    else:
        raise AssertionError(f"unhandled command: {cmd}")

    return out


def _exit_code_through_dbos_shutdown(code: int) -> int:
    """Destroy a runtime this command launched; hard-exit past what survives.

    `main` is the process boundary, so `main` owns the stop; `execute_argv`
    deliberately does not, because the resident daemon's runtime outlives any
    one command. Reached through `sys.modules` rather than an import, because
    most commands never touch DBOS and paying the heaviest import in the
    repository on every exit path just to discover there is nothing to stop
    would be absurd. The boundary itself, and why a surviving thread costs an
    `os._exit`, is `dbos_app.exit_code_after_runtime_shutdown`.
    """

    dbos_app = sys.modules.get("local_first_agent_os.dbos_app")
    if dbos_app is None:
        return code
    return dbos_app.exit_code_after_runtime_shutdown(code)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    set_root(args.root)

    if args.cmd == "serve":
        serve_mcp(args.transport, audience=args.audience)
        return _exit_code_through_dbos_shutdown(0)

    try:
        out = dispatch(args)
    except Exception as e:
        print_json({"ok": False, "error": type(e).__name__, "message": str(e)})
        return _exit_code_through_dbos_shutdown(1)
    print_json(out)
    if not args.no_next_commands:
        print_next_commands(args.cmd, out)
    return _exit_code_through_dbos_shutdown(0 if out.get("ok", False) else 2)


def execute_argv(argv: Sequence[str]) -> dict[str, Any]:
    """Run one command in this process and return the payload the CLI prints.

    Same grammar, same handlers, same result shape as running the script, minus
    the interpreter start and the JSON round trip. It does not touch the root:
    the caller has already pointed the process at a ledger, and re-deriving that
    here from a different channel is exactly how two transports drift apart.

    `serve` is refused rather than handled. It is the MCP server's own process
    lifetime, which is not something a command can return from.
    """

    parser = build_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit:
        # argparse exits the interpreter on a bad command line. In a child that
        # is a failed process; here it would take the daemon down with it.
        return {
            "ok": False,
            "error": "ArgumentError",
            "message": f"coordination command rejected by the parser: {list(argv)}",
        }
    if args.cmd == "serve":
        raise ValueError("serve cannot run through the in-process transport")
    try:
        return dispatch(args)
    except Exception as failure:
        # An unreachable ledger is not a rejected command, and flattening it into
        # one says something false: `{"ok": false}` means the ledger considered
        # this and declined it. It also destroys the exception type, which is the
        # only evidence a resident loop has for telling an outage it should wait
        # out apart from a defect it must not. Letting it through costs the CLI
        # nothing - `main` still prints it and exits non-zero - and it is what
        # lets the in-process transport hand callers the real failure.
        if ledger_unavailable(failure):
            raise
        return {
            "ok": False,
            "error": type(failure).__name__,
            "message": str(failure),
        }


if __name__ == "__main__":
    raise SystemExit(main())


"""
MCP client config:

{
  "mcpServers": {
    "agent-coordination": {
      "command": "python",
      "args": [
        "/absolute/path/to/agent_coordination_mcp.py",
        "--root", "/absolute/path/to/repo",
        "serve"
      ],
      "env": { "AGENT_SESSION_ID": "claude-1" }
    }
  }
}

Runtime state lives in the configured Postgres ledger.
The repo-local SQLite adapter is reserved for isolated tests.
"""
