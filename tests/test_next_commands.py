# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The follow-up commands printed beside a result.

Two properties carry this feature, and both are tested here rather than assumed.

A printed command must be runnable as printed, or it must say why it is not. A
suggestion that fails when pasted costs the operator more attention than no
suggestion at all.

And the refusals must be the real ones. The value of naming
``settled_adoption_dispatch_not_done`` before the operator reaches for that verb
depends entirely on the code being the code the verb would actually raise, so
the refusal predicates are tested against the shapes the projection really
produces.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from local_first_agent_os.contracts import DispatchIntentStatus
from local_first_agent_os.coordination import cli
from local_first_agent_os.coordination.contracts import CoordinationFlag
from local_first_agent_os.work_units.events import (
    DecisionKindMismatch,
    DecisionRequestKind,
    OperatorDecision,
    decision_outcome,
)
from local_first_agent_os.work_units.lifecycle import (
    LifecyclePhase,
    MilestoneExecutionStatus,
    PhaseStatus,
    WorkUnitStatus,
)
from local_first_agent_os.work_units.next_commands import (
    _DISPOSITIONS,
    _PERMITTED_DECISIONS,
    IncompleteNextCommandRules,
    NextCommand,
    NextCommandStatus,
    _require_total,
    decision_idempotency_key,
    next_commands_for,
    next_commands_for_view,
)
from local_first_agent_os.work_units.projection import (
    BlockingCondition,
    MilestoneView,
    PendingDecisionView,
    PhaseView,
    WorkUnitView,
)

WORK_UNIT_ID = "ac5337cbb811aa8f45bbcce4fbf9cf14"


def _milestone(
    stable_key: str = "1",
    *,
    status: MilestoneExecutionStatus = MilestoneExecutionStatus.RUNNING,
    dispatch_status: DispatchIntentStatus | None = None,
    dispatch_intent_id: str | None = None,
    produced: tuple[str, ...] = (),
    failure_code: str | None = None,
) -> MilestoneView:
    return MilestoneView(
        stable_key=stable_key,
        title=f"milestone {stable_key}",
        phase=LifecyclePhase.IMPLEMENT,
        ordinal=int(stable_key),
        executor_kind="implement.code_change",
        status=status,
        attempt=1,
        requires_operator_approval=False,
        milestone_execution_id=f"mex_{stable_key}",
        produced_artifacts=produced,
        dispatch_intent_id=dispatch_intent_id,
        dispatch_status=dispatch_status,
        failure_code=failure_code,
    )


def _view(
    *,
    status: WorkUnitStatus = WorkUnitStatus.RUNNING,
    blocking: BlockingCondition | None = None,
    milestones: tuple[MilestoneView, ...] = (),
    pending_decisions: tuple[PendingDecisionView, ...] = (),
) -> WorkUnitView:
    return WorkUnitView(
        work_unit_id=WORK_UNIT_ID,
        title="Prompt view compaction and token budgets",
        status=status,
        current_phase="IMPLEMENT",
        design_doc_revision_id="ddr_0361e3941b384e6811da76b4",
        compiled_plan_revision_id="cpr_49057eb977bd823c2310c538",
        compiled_plan_hash="eea3e9ab1369bc9a9dec92918e48ebe0c0a1ea6219ffe81609813a16764949f7",
        lifecycle_profile="engineering.v1",
        lifecycle_profile_version=1,
        root_workflow_id=f"work-unit:{WORK_UNIT_ID}",
        created_at="2026-08-16T00:00:00Z",
        phases=(
            PhaseView(
                phase=LifecyclePhase.IMPLEMENT,
                status=PhaseStatus.RUNNING,
                milestone_keys=tuple(item.stable_key for item in milestones),
            ),
        ),
        milestones=milestones,
        blocking=blocking or BlockingCondition(kind="NONE", detail="nothing is blocking this work"),
        pending_decisions=pending_decisions,
        artifacts=(),
        recent_events=(),
    )


def _blocked_view() -> WorkUnitView:
    """The shape `67122ee7d6a5` really has: wait-elapsed, no review, FAILED intent."""

    milestone = _milestone(
        "1",
        status=MilestoneExecutionStatus.BLOCKED,
        dispatch_status=DispatchIntentStatus.FAILED,
        dispatch_intent_id="9f75e62a-67d9-4a60-a0d6-4a37a813d473",
        produced=("dispatch_failure_evidence",),
        failure_code="dispatch_wait_elapsed",
    )
    return _view(
        status=WorkUnitStatus.BLOCKED,
        blocking=BlockingCondition(
            kind="BLOCKED_MILESTONE",
            detail="a milestone stopped without finishing and needs recovery",
            milestone_keys=("1",),
        ),
        milestones=(milestone,),
    )


def _find(commands: tuple[NextCommand, ...], verb: str) -> NextCommand:
    matches = [item for item in commands if verb in item.command]
    assert matches, f"no next command mentions {verb}"
    return matches[0]


# --- the invariant: a printed command runs ---------------------------------


def test_a_ready_command_may_not_carry_a_placeholder() -> None:
    """The model refuses it, so no construction site can print an unrunnable command."""

    with pytest.raises(ValueError, match="runnable as printed"):
        NextCommand(
            command="agent-ledger resume_work_unit <work_unit_id>",
            intent="resume",
            status=NextCommandStatus.READY,
        )


def test_a_refused_command_must_say_why() -> None:
    with pytest.raises(ValueError, match="must say why"):
        NextCommand(
            command="agent-ledger resume_work_unit abc",
            intent="resume",
            status=NextCommandStatus.REFUSED,
        )


@pytest.mark.parametrize("status", list(WorkUnitStatus))
def test_every_status_produces_only_runnable_ready_commands(status: WorkUnitStatus) -> None:
    """Totality, checked through the builder rather than only at the table.

    The disposition table being total does not by itself prove the builder
    handles every status, because the builder also reads blocking kinds and
    milestone rows. Driving all eleven through it does.
    """

    result = next_commands_for_view(
        _view(status=status, milestones=(_milestone("1", status=MilestoneExecutionStatus.BLOCKED),))
    )
    assert result.headline.startswith(status.value)
    for command in result.ready:
        assert "<" not in command.command


def test_a_missing_status_stops_the_import() -> None:
    """The guard that makes the table total, exercised on a copy.

    Against a copy rather than by mutating `_DISPOSITIONS`, because a test that
    breaks the module's own table would leave every later test in the file
    running against a half-built rule set.
    """

    partial = {k: v for k, v in _DISPOSITIONS.items() if k is not WorkUnitStatus.BLOCKED}
    with pytest.raises(IncompleteNextCommandRules, match="BLOCKED"):
        _require_total(WorkUnitStatus, partial, "test table")


# --- the near-misses this exists to prevent --------------------------------


def test_settled_adoption_is_refused_when_the_intent_failed() -> None:
    """The documented hour-long near-miss, named before the operator reaches for it.

    `dispatch_wait_elapsed` reads exactly like the condition
    `adopt_settled_work_unit_dispatch` exists for. The intent behind it is FAILED
    rather than DONE, so the verb refuses; this says so from the same payload.
    """

    result = next_commands_for_view(_blocked_view())
    adoption = _find(result.commands, "adopt_settled_work_unit_dispatch")
    assert adoption.status is NextCommandStatus.REFUSED
    assert adoption.refusal_code == "settled_adoption_dispatch_not_done"
    assert "FAILED" in (adoption.reason or "")


def test_settled_adoption_is_ready_when_the_intent_is_done() -> None:
    view = _view(
        status=WorkUnitStatus.BLOCKED,
        blocking=BlockingCondition(kind="BLOCKED_MILESTONE", detail="", milestone_keys=("1",)),
        milestones=(
            _milestone(
                "1",
                status=MilestoneExecutionStatus.BLOCKED,
                dispatch_status=DispatchIntentStatus.DONE,
                dispatch_intent_id="intent-1",
            ),
        ),
    )
    adoption = _find(next_commands_for_view(view).commands, "adopt_settled_work_unit_dispatch")
    assert adoption.status is NextCommandStatus.READY


def test_review_recovery_is_refused_when_no_review_ever_ran() -> None:
    """ "A review that never ran has nothing to reparse", made automatic.

    This was a hand check against `list_work_unit_artifacts`. The artifact list
    is already on the milestone row, so it costs no query.
    """

    result = next_commands_for_view(_blocked_view())
    recovery = _find(result.commands, "recover_unparsed_staff_review")
    assert recovery.status is NextCommandStatus.REFUSED
    assert recovery.refusal_code == "staff_review_missing"
    assert "dispatch_failure_evidence" in (recovery.reason or "")


def test_review_recovery_is_unproved_when_a_review_exists() -> None:
    """A review that did run is not refused here: this view cannot read its verdict."""

    view = _view(
        status=WorkUnitStatus.BLOCKED,
        blocking=BlockingCondition(kind="BLOCKED_MILESTONE", detail="", milestone_keys=("1",)),
        milestones=(
            _milestone(
                "1",
                status=MilestoneExecutionStatus.BLOCKED,
                dispatch_status=DispatchIntentStatus.FAILED,
                dispatch_intent_id="intent-1",
                produced=("worktree_commit_checkpoint", "review_result"),
            ),
        ),
    )
    recovery = _find(next_commands_for_view(view).commands, "recover_unparsed_staff_review")
    assert recovery.status is NextCommandStatus.UNPROVED


def test_integrated_adoption_never_claims_to_be_ready() -> None:
    """It needs a commit sha nobody here has, so it is UNPROVED by construction."""

    adoption = _find(
        next_commands_for_view(_blocked_view()).commands,
        "adopt_integrated_work_unit_milestone",
    )
    assert adoption.status is NextCommandStatus.UNPROVED
    assert "<commit_sha>" in adoption.command


def test_a_succeeded_work_unit_offers_no_recovery_verbs() -> None:
    result = next_commands_for_view(_view(status=WorkUnitStatus.SUCCEEDED))
    assert not [item for item in result.commands if "adopt_" in item.command]
    assert not [item for item in result.commands if "cancel_work_unit" in item.command]


# --- the decision commands -------------------------------------------------


@pytest.mark.parametrize("kind", list(DecisionRequestKind))
def test_offered_decisions_are_exactly_the_ones_the_resolver_accepts(
    kind: DecisionRequestKind,
) -> None:
    """The two-sided guard for `_PERMITTED_DECISIONS`.

    `events.decision_outcome` is the authority on which answer resolves which
    request. Offering one it rejects would print a READY command that cannot run,
    so this proves the table against the authority in both directions rather than
    trusting a copy of it.
    """

    permitted = set(_PERMITTED_DECISIONS[kind])
    for decision in OperatorDecision:
        if decision in permitted:
            assert decision_outcome(kind, decision) is not None
        else:
            with pytest.raises(DecisionKindMismatch):
                decision_outcome(kind, decision)


def test_a_pending_approval_offers_both_verdicts_with_distinct_keys() -> None:
    view = _view(
        status=WorkUnitStatus.WAITING_FOR_OPERATOR,
        blocking=BlockingCondition(kind="OPERATOR_DECISION", detail="", milestone_keys=("1",)),
        milestones=(_milestone("1", status=MilestoneExecutionStatus.WAITING_FOR_OPERATOR),),
        pending_decisions=(
            PendingDecisionView(
                request_id="req-42",
                request_kind=DecisionRequestKind.APPROVAL.value,
                prompt="merge the reviewed commit?",
                milestone_execution_id="mex_1",
                created_at="2026-08-16T00:00:00Z",
            ),
        ),
    )
    decisions = [
        item
        for item in next_commands_for_view(view).commands
        if "submit_work_unit_decision" in item.command
    ]
    assert len(decisions) == 2
    assert all(item.status is NextCommandStatus.READY for item in decisions)
    keys = {item.command.rsplit(" ", 1)[-1] for item in decisions}
    assert len(keys) == 2, "APPROVED and DENIED must not share an idempotency key"


def test_idempotency_keys_are_unique_per_request_and_decision() -> None:
    """There is a partial unique index on the key; a reused string collides."""

    keys = {
        decision_idempotency_key(request, decision)
        for request in ("req-1", "req-2")
        for decision in OperatorDecision
    }
    assert len(keys) == 2 * len(OperatorDecision)


def test_an_unknown_request_kind_refuses_to_guess_a_verdict() -> None:
    """A kind this version cannot read gets a placeholder, never a confident verdict."""

    view = _view(
        status=WorkUnitStatus.WAITING_FOR_OPERATOR,
        pending_decisions=(
            PendingDecisionView(
                request_id="req-1",
                request_kind="SOMETHING_NEW",
                prompt="?",
                milestone_execution_id=None,
                created_at="2026-08-16T00:00:00Z",
            ),
        ),
    )
    decision = _find(next_commands_for_view(view).commands, "submit_work_unit_decision")
    assert decision.status is NextCommandStatus.UNPROVED
    assert "SOMETHING_NEW" in (decision.reason or "")


# --- the envelope-level dispatch -------------------------------------------


def test_a_compile_carries_both_ids_into_a_runnable_start() -> None:
    """The affordance's original case: no id is retyped."""

    payload = {
        "ok": True,
        "compiled_plan_revision_id": "cpr_9ceed24553bffc271eeeebd4",
        "plan_hash": "a03674fcd4784f26b7d8437cf7efc818d0edb4a43502dd09cda1ba4f9a3c7a3c",
        "validation_status": "VALID",
        "runnable": True,
        "execution_blockers": [],
    }
    result = next_commands_for("compile_design_doc", payload)
    assert result is not None
    start = _find(result.commands, "start_work_unit")
    assert start.status is NextCommandStatus.READY
    assert payload["compiled_plan_revision_id"] in start.command
    assert payload["plan_hash"] in start.command


def test_a_blocked_compile_refuses_the_start_and_names_the_blocker() -> None:
    result = next_commands_for(
        "compile_design_doc",
        {
            "ok": True,
            "compiled_plan_revision_id": "cpr_1",
            "plan_hash": "h",
            "validation_status": "VALID",
            "runnable": False,
            "execution_blockers": ["design doc declares no target project"],
        },
    )
    assert result is not None
    start = _find(result.commands, "start_work_unit")
    assert start.status is NextCommandStatus.REFUSED
    assert "target project" in (start.reason or "")


def test_a_failed_result_suggests_nothing() -> None:
    """A refusal holds an error code, not the ids these builders read."""

    assert next_commands_for("compile_design_doc", {"ok": False, "error": "compile_failed"}) is None


def test_an_unknown_command_suggests_nothing() -> None:
    assert next_commands_for("list_sessions", {"ok": True, "sessions": []}) is None


def test_a_start_whose_enqueue_is_pending_offers_the_drainer() -> None:
    """The "no active DBOS runtime" reply is not a failure, but it does mean a command."""

    result = next_commands_for(
        "start_work_unit",
        {
            "ok": True,
            "work_unit_id": WORK_UNIT_ID,
            "dispatch": [{"delivered": False, "reason": "no active DBOS runtime"}],
        },
    )
    assert result is not None
    drain = _find(result.commands, "drain_work_unit_enqueues")
    assert drain.status is NextCommandStatus.READY


def test_the_index_names_only_the_work_units_that_need_attention() -> None:
    result = next_commands_for(
        "list_work_units",
        {
            "ok": True,
            "work_units": [
                {"work_unit_id": "a", "status": "RUNNING", "title": "live"},
                {"work_unit_id": "b", "status": "CANCELLED", "title": "done"},
            ],
        },
    )
    assert result is not None
    assert len(result.commands) == 1
    assert "get_work_unit a" in result.commands[0].command


# --- the rendering ---------------------------------------------------------


@pytest.mark.parametrize(
    "command,payload",
    [
        ("start_work_unit", {"ok": True, "work_unit_id": WORK_UNIT_ID}),
        ("resume_work_unit", {"ok": True, "work_unit_id": WORK_UNIT_ID}),
        ("cancel_work_unit", {"ok": True, "work_unit_id": WORK_UNIT_ID}),
        (
            "resume_work_unit",
            {"ok": True, "work_unit_id": WORK_UNIT_ID, "delivered": False, "reason": "no runtime"},
        ),
    ],
)
def test_a_headline_never_reads_as_a_command(command: str, payload: dict[str, Any]) -> None:
    """No headline may look like `verb <argument>`.

    `WorkUnit ac5337...` was a real headline, and an operator copying the line
    without its `#` got `zsh: command not found: WorkUnit`. A comment marker is
    the wrong thing to depend on for this: terminals drop a leading `#` on a
    double-click selection, and interactive zsh does not treat `#` as a comment
    unless `interactive_comments` is set. Prose that cannot be mistaken for argv
    in the first place is what actually holds, so the identifier goes after the
    words rather than in the argument position.
    """

    result = next_commands_for(command, payload)
    assert result is not None
    assert not re.match(r"^\w[\w-]*\s+[0-9a-f]{12,}", result.headline), (
        f"headline {result.headline!r} reads as a command with an argument"
    )


def test_a_resume_that_delivered_nothing_says_so() -> None:
    """`ok: true` with `delivered: false` means the WorkUnit is still parked.

    The verb returns success, returns milestones to READY, and writes no outbox
    row, so nothing will ever pick the continuation up. Reporting only "re-read
    the WorkUnit" there reads as though the resume worked.
    """

    result = next_commands_for(
        "resume_work_unit",
        {
            "ok": True,
            "work_unit_id": WORK_UNIT_ID,
            "delivered": False,
            "reason": "no active DBOS runtime; resume again from a durable runtime",
        },
    )
    assert result is not None
    assert "nothing is running it" in result.headline
    assert "no active DBOS runtime" in (result.detail or "")
    inline = _find(result.commands, "--inline")
    assert inline.status is NextCommandStatus.READY


@pytest.mark.parametrize(
    "view",
    [
        _blocked_view(),
        _view(status=WorkUnitStatus.RUNNING),
        _view(status=WorkUnitStatus.SUCCEEDED),
    ],
    ids=["blocked", "running", "succeeded"],
)
def test_every_uncommented_line_is_a_ready_command(view: WorkUnitView) -> None:
    """The whole block is shell: comments explain, and only READY commands run.

    This is the property that makes the output unambiguous. Indentation alone
    used to separate a command from the prose under it, and the first person to
    read this output asked whether a command and its `but` line were one command.
    A refused verb is now commented out, so pasting a group cannot run one.
    """

    rendered = cli.render_next_commands(next_commands_for_view(view))
    runnable = [
        line.strip()
        for line in rendered.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    expected = [item.command for item in next_commands_for_view(view).ready]
    assert runnable == expected


def test_a_refused_command_is_commented_out() -> None:
    rendered = cli.render_next_commands(next_commands_for_view(_blocked_view()))
    assert "  # agent-ledger adopt_settled_work_unit_dispatch" in rendered
    assert "\n  agent-ledger adopt_settled_work_unit_dispatch" not in rendered


def test_the_command_is_the_last_line_of_its_block() -> None:
    """The line to copy sits at the bottom of its comment block, not buried above it."""

    rendered = cli.render_next_commands(next_commands_for_view(_blocked_view()))
    lines = rendered.splitlines()
    index = lines.index("  agent-ledger resume_work_unit " + WORK_UNIT_ID)
    assert lines[index - 1].strip().startswith("#")
    assert lines[index + 1] == ""


# --- the CLI surface -------------------------------------------------------


def _run_cli(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], *argv: str) -> int:
    monkeypatch.setattr(cli, "dispatch", lambda args: payload)
    monkeypatch.setattr(cli, "set_root", lambda root: None)
    return cli.main(list(argv))


def test_stdout_stays_parseable_json_and_the_commands_go_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The documented watch loop pipes stdout into a JSON parser; it must keep working."""

    _run_cli(
        monkeypatch,
        {
            "ok": True,
            "compiled_plan_revision_id": "cpr_1",
            "plan_hash": "h",
            "validation_status": "VALID",
            "runnable": True,
            "execution_blockers": [],
        },
        "compile_design_doc",
        "docs/x.md",
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["compiled_plan_revision_id"] == "cpr_1"
    assert "next commands" in captured.err
    assert "start_work_unit cpr_1" in captured.err


def test_no_next_commands_suppresses_the_suggestions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_cli(
        monkeypatch,
        {
            "ok": True,
            "compiled_plan_revision_id": "cpr_1",
            "plan_hash": "h",
            "validation_status": "VALID",
            "runnable": True,
            "execution_blockers": [],
        },
        "--no-next-commands",
        "compile_design_doc",
        "docs/x.md",
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert captured.err == ""


def test_the_environment_can_suppress_them_for_a_whole_shell(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The flag only parses before the subcommand; the variable has no such rule."""

    monkeypatch.setenv("LOCAL_AGENT_NO_NEXT_COMMANDS", "1")
    _run_cli(monkeypatch, {"ok": True, "work_unit_id": "x"}, "start_work_unit", "cpr_1")
    assert capsys.readouterr().err == ""


def test_an_unset_environment_leaves_the_commands_on(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("LOCAL_AGENT_NO_NEXT_COMMANDS", raising=False)
    _run_cli(monkeypatch, {"ok": True, "work_unit_id": "x"}, "start_work_unit", "cpr_1")
    assert "next commands" in capsys.readouterr().err


def test_the_suppression_flag_is_declared_in_the_flag_enum() -> None:
    """`test_coordination_contracts` asserts parser flags equal `CoordinationFlag`.

    Named here too, because that test reports the drift as a set difference and
    this one says which flag and why it exists.
    """

    assert CoordinationFlag.NO_NEXT_COMMANDS.value == "--no-next-commands"


def test_a_renderer_defect_never_fails_the_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The result is already correct and already printed; a convenience must not undo that."""

    def explode(command: str, payload: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "next_commands_for", explode)
    code = _run_cli(monkeypatch, {"ok": True, "work_unit_id": "x"}, "start_work_unit", "cpr_1")
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["ok"] is True
    assert "next commands unavailable" in captured.err
