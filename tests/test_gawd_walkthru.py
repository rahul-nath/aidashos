# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from local_first_agent_os.gawd_walkthru import (
    SECTIONS,
    GawdWalkthruStore,
    SummaryProposal,
    WalkthruError,
    WalkthruSection,
    parse_summary_proposal,
)
from local_first_agent_os.new_project_intake import parse_sparse_gawd_draft


def _proposal(section_id: str, verbatim: str) -> SummaryProposal:
    return SummaryProposal(
        summary=f"Accepted {section_id}: {verbatim}",
        suggestions=(f"Possible follow-up for {section_id}",),
    )


def test_walkthru_preserves_verbatim_summary_and_suggestions_separately(
    tmp_path: Path,
) -> None:
    store = GawdWalkthruStore(tmp_path)
    started = store.start(target_project_id=None, operation_id="start")
    walkthru_id = started["walkthru_id"]
    verbatim = "Public Copy Project"

    proposed = store.answer(
        walkthru_id,
        verbatim,
        operation_id="answer-project",
        summarize=lambda section, answer: _proposal(section.section_id, answer),
    )

    assert proposed["proposal"]["verbatim"] == verbatim
    assert proposed["proposal"]["summary"] == verbatim
    assert proposed["proposal"]["suggestions"] == []
    session = store.read_session(walkthru_id)
    assert session["responses"][0]["verbatim"] == verbatim
    assert session["responses"][0]["model_summary"] == verbatim
    assert session["responses"][0]["accepted_summary"] is None


def test_walkthru_answer_replays_a_completed_operation_without_resummarizing(
    tmp_path: Path,
) -> None:
    store = GawdWalkthruStore(tmp_path)
    started = store.start(target_project_id=None, operation_id="start")
    walkthru_id = started["walkthru_id"]
    summarize_calls: list[str] = []

    def summarize(section: WalkthruSection, answer: str) -> SummaryProposal:
        summarize_calls.append(answer)
        return _proposal(section.section_id, answer)

    # The project section carries metadata verbatim and never reaches the
    # summarizer, so clear it before exercising a section that does.
    store.answer(
        walkthru_id,
        "Public Copy Project",
        operation_id="answer-project",
        summarize=summarize,
    )
    store.accept_proposed_summary(walkthru_id, operation_id="accept-project")

    first = store.answer(
        walkthru_id,
        "Four hours total",
        operation_id="answer-budget",
        summarize=summarize,
    )
    replayed = store.answer(
        walkthru_id,
        "Four hours total",
        operation_id="answer-budget",
        summarize=summarize,
    )

    assert replayed == first
    # The recorded result short-circuits before the summarizer, so a retried
    # answer never pays for a second model call.
    assert summarize_calls == ["Four hours total"]
    assert len(store.read_session(walkthru_id)["responses"]) == 2


def test_walkthru_accept_is_idempotent_and_survives_reload(tmp_path: Path) -> None:
    store = GawdWalkthruStore(tmp_path)
    started = store.start(target_project_id=None, operation_id="start")
    walkthru_id = started["walkthru_id"]
    store.answer(
        walkthru_id,
        "Public Copy Project",
        operation_id="answer-project",
        summarize=lambda section, answer: _proposal(section.section_id, answer),
    )

    first = store.accept_proposed_summary(walkthru_id, operation_id="accept-project")
    replayed = GawdWalkthruStore(tmp_path).accept_proposed_summary(
        walkthru_id,
        operation_id="accept-project",
    )

    assert replayed == first
    assert replayed["completed_sections"] == 1
    assert replayed["section"]["section_id"] == "time_budget"


def test_latest_unfinished_walkthru_returns_most_recent_session(tmp_path: Path) -> None:
    store = GawdWalkthruStore(tmp_path)
    first = store.start(target_project_id=None, operation_id="start-first")
    second = store.start(target_project_id=None, operation_id="start-second")

    latest = GawdWalkthruStore(tmp_path).find_latest_incomplete()

    assert latest is not None
    assert latest["walkthru_id"] == second["walkthru_id"]
    assert latest["walkthru_id"] != first["walkthru_id"]


def test_walkthru_rejects_invalid_state_transition(tmp_path: Path) -> None:
    store = GawdWalkthruStore(tmp_path)
    started = store.start(target_project_id=None, operation_id="start")

    with pytest.raises(WalkthruError, match="requires awaiting_review"):
        store.accept_proposed_summary(started["walkthru_id"], operation_id="accept-too-early")


def test_walkthru_saves_verbatim_before_summary_model_runs(tmp_path: Path) -> None:
    store = GawdWalkthruStore(tmp_path)
    started = store.start(target_project_id=None, operation_id="start")
    walkthru_id = started["walkthru_id"]
    store.answer(
        walkthru_id,
        "Public Copy Project",
        operation_id="answer-project",
        summarize=lambda section, answer: _proposal(section.section_id, answer),
    )
    store.accept_proposed_summary(walkthru_id, operation_id="accept-project")

    def fail_summary(_section, _answer):
        raise RuntimeError("model offline")

    with pytest.raises(RuntimeError, match="model offline"):
        store.answer(
            walkthru_id,
            "Four hours total",
            operation_id="answer-budget",
            summarize=fail_summary,
        )

    saved = GawdWalkthruStore(tmp_path).read_session(walkthru_id)
    assert saved["state"] == "awaiting_summary"
    assert saved["responses"][-1]["verbatim"] == "Four hours total"
    assert saved["responses"][-1]["status"] == "awaiting_summary"

    retried = store.answer(
        walkthru_id,
        "Four hours total",
        operation_id="answer-budget",
        summarize=lambda section, answer: _proposal(section.section_id, answer),
    )
    assert retried["state"] == "awaiting_review"
    assert retried["proposal"]["verbatim"] == "Four hours total"


def test_walkthru_finishes_as_existing_parseable_sparse_gawd(tmp_path: Path) -> None:
    store = GawdWalkthruStore(tmp_path)
    started = store.start(target_project_id="public-copy", operation_id="start")
    walkthru_id = started["walkthru_id"]

    answers = {
        "project": "Public Copy Project",
        "time_budget": "One day to scope, three days to build, one day to verify.",
        "theory": "A curated import turns selected private source into a clean public repo.",
        "why": "It avoids exposing private history while preserving useful system pieces.",
        "happy_path": "Select components, sanitize them, test the snapshot, then approve publish.",
        "scope": "Include generic orchestration. Exclude Workflowy and personal data.",
        "core_design": "A source manifest produces reviewed files in an empty staging repo.",
        "failure": "Private names or secrets reach the public snapshot.",
        "verification": "Secret scan, tests, and a human diff review all pass.",
        "milestones": (
            "Import scaffold, import engine, validate, then request publication approval."
        ),
        "operational_contract": "Every copied file needs provenance and scan evidence.",
        "rollout": "Keep the staging repo private until a human publication gate.",
        "risks": "Automated scans can miss contextual personal information.",
        "decisions": "Use a new repository and transparent curated-import commits.",
        "deferred": "Workflowy integrations and promotion can come later.",
    }
    for index, section in enumerate(SECTIONS):
        store.answer(
            walkthru_id,
            answers[section.section_id],
            operation_id=f"answer-{index}",
            summarize=lambda current, answer: SummaryProposal(summary=answer),
        )
        store.accept_proposed_summary(walkthru_id, operation_id=f"accept-{index}")

    ready = store.read_status(walkthru_id)
    edited_theory = "A reviewed curated import contract creates a clean public repository."
    store.edit_accepted_summary(
        walkthru_id,
        "theory",
        edited_theory,
        operation_id="edit-theory",
    )
    finished = store.write_completed_sparse_gawd_draft(walkthru_id, operation_id="finish")
    draft = parse_sparse_gawd_draft(Path(finished["draft_path"]))

    assert ready["state"] == "ready_to_finish"
    assert finished["state"] == "finished"
    assert finished["execution_started"] is False
    assert finished["next_command"].endswith("--target-project-id public-copy")
    assert draft.project == "Public Copy Project"
    assert draft.theory == edited_theory
    assert draft.golden_flow
    sidecar = store.read_session(walkthru_id)
    assert sidecar["responses"][4]["verbatim"] == answers["happy_path"]
    assert sidecar["responses"][2]["verbatim"] == answers["theory"]
    assert sidecar["responses"][2]["operator_summary_revisions"][0]["text"] == edited_theory


def test_summary_parser_falls_back_to_verbatim_without_merging_model_prose() -> None:
    proposal = parse_summary_proposal(
        {"text": "Here is a nice unstructured answer."},
        verbatim="Keep this exact uncertainty.",
    )

    assert proposal.summary == "Keep this exact uncertainty."
    assert proposal.suggestions == ()
    assert proposal.method == "fallback_verbatim"
