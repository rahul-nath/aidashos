# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .new_project_intake import create_sparse_gawd_draft_file, render_sparse_gawd_draft

SCHEMA_VERSION = "gawd_walkthru.v1"
WalkthruState = Literal[
    "awaiting_answer",
    "awaiting_summary",
    "awaiting_review",
    "ready_to_finish",
    "finished",
]


@dataclass(frozen=True)
class WalkthruSection:
    section_id: str
    heading: str | None
    label: str
    question: str
    guidance: str


@dataclass(frozen=True)
class SummaryProposal:
    summary: str
    suggestions: tuple[str, ...] = ()
    method: str = "model"


SECTIONS: tuple[WalkthruSection, ...] = (
    WalkthruSection(
        "project",
        None,
        "Project name",
        "What should this project be called? A working title is fine.",
        "Use a short name that distinguishes this project from the private source repo.",
    ),
    WalkthruSection(
        "time_budget",
        "Time Budget",
        "Time budget",
        "What time box do you want for scoping, building, and verifying this version?",
        "Approximate hours or days are enough. Use --skip to keep the Mini-GAWD table.",
    ),
    WalkthruSection(
        "theory",
        "1. Theory of the System",
        "Theory of the system",
        "In your own words, what kind of system is this and what does it transform into what?",
        "A rough computational shape is enough; implementation details can wait.",
    ),
    WalkthruSection(
        "why",
        "2. Why This Exists",
        "Why this exists",
        "What concrete pain should this remove, and for whom?",
        "Describe the present frustration and the useful change, not a market pitch.",
    ),
    WalkthruSection(
        "happy_path",
        "3. Happy Path / Golden Flow",
        "Happy path",
        "Talk me through one successful run from the starting state to done.",
        "Tell the story naturally. The summary can recover ordered steps for milestone derivation.",
    ),
    WalkthruSection(
        "scope",
        "4. This Version - Scope & Non-Goals",
        "Scope and non-goals",
        "What belongs in this version, and what should it deliberately not try to do?",
        "Uncertain or deferred ideas can be named without being pulled into scope.",
    ),
    WalkthruSection(
        "core_design",
        "5. Core Design",
        "Core design",
        "What are the main pieces, durable records, and lifecycle as you currently imagine them?",
        "It is okay to answer conceptually; suggestions will be kept separate from your contract.",
    ),
    WalkthruSection(
        "failure",
        "6. The Failure That Matters Most",
        "Most important failure",
        "What failure would make this version misleading, unsafe, or not worth shipping?",
        "Pick the failure whose prevention or detection matters most.",
    ),
    WalkthruSection(
        "verification",
        "7. Verification",
        "Verification",
        "What observable proof would convince you this version actually works?",
        "Mention real commands, artifacts, examples, or human checks if you know them.",
    ),
    WalkthruSection(
        "milestones",
        "8. Execution Milestones",
        "Execution milestones",
        "Do you already care about a particular build order or approval gate?",
        "You may skip this. Staff can derive milestones from the accepted happy path.",
    ),
    WalkthruSection(
        "operational_contract",
        "9. Operational Contract",
        "Operational contract",
        (
            "What operating constraints or promises matter: limits, retries, evidence, "
            "dependencies, access, cost, or service levels?"
        ),
        "Say only what you know. The summary will organize it without inventing requirements.",
    ),
    WalkthruSection(
        "rollout",
        "10. Rollout / Migration / Rollback",
        "Rollout and rollback",
        "How should this be introduced, and what should happen if it goes wrong?",
        "Include any manual publication or migration gate that must remain human-approved.",
    ),
    WalkthruSection(
        "risks",
        "11. Risk Synthesis / Known Limitations",
        "Risks and limitations",
        (
            "What are you least certain about, and where should this design stop "
            "claiming to be sufficient?"
        ),
        "Distinguish known limitations from model suggestions or future possibilities.",
    ),
    WalkthruSection(
        "decisions",
        "12. Decision Log",
        "Decision log",
        "Which decisions have you already made, and why?",
        (
            "Include naming, privacy, architecture, or release decisions that downstream "
            "agents must not relitigate."
        ),
    ),
    WalkthruSection(
        "deferred",
        "13. If I Had 2 More Weeks",
        "Deferred work",
        "What would be valuable later but should not bias or enlarge this version?",
        "This is a parking lot, not an implied commitment.",
    ),
)

SECTION_BY_ID = {section.section_id: section for section in SECTIONS}


class WalkthruError(ValueError):
    pass


class GawdWalkthruStore:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.directory = self.repo_root / "docs" / "gawd_drafts"
        self.directory.mkdir(parents=True, exist_ok=True)

    def start(
        self,
        *,
        target_project_id: str | None,
        create_target_id: str | None = None,
        operation_id: str,
    ) -> dict[str, Any]:
        draft = create_sparse_gawd_draft_file(self.repo_root)
        walkthru_id = f"gawd-walkthru-{uuid4().hex[:12]}"
        now = _now()
        session = {
            "schema_version": SCHEMA_VERSION,
            "walkthru_id": walkthru_id,
            "draft_id": draft.draft_id,
            "draft_path": str(draft.path),
            "target_project_id": target_project_id,
            "create_target_id": create_target_id,
            "state": "awaiting_answer",
            "current_section_index": 0,
            "responses": [],
            "created_at": now,
            "updated_at": now,
            "last_operation_id": None,
            "last_result": None,
        }
        result = self._build_operation_result(session, status="walkthru_started")
        self._record_result_and_write_session(session, operation_id, result)
        return result

    def read_session(self, walkthru_id: str) -> dict[str, Any]:
        return self._read_session_file(self._build_session_path(walkthru_id))

    def find_latest_incomplete(self) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for path in self.directory.glob("gawd-walkthru-*.json"):
            try:
                session = self._read_session_file(path)
            except (OSError, json.JSONDecodeError, WalkthruError):
                continue
            if session["state"] != "finished":
                candidates.append(session)
        if not candidates:
            return None
        return max(candidates, key=lambda item: str(item.get("updated_at") or ""))

    def read_status(self, walkthru_id: str) -> dict[str, Any]:
        session = self.read_session(walkthru_id)
        return self._build_operation_result(
            session, status="walkthru_status", include_responses=True
        )

    def answer(
        self,
        walkthru_id: str,
        verbatim: str,
        *,
        operation_id: str,
        summarize: Callable[[WalkthruSection, str], SummaryProposal],
    ) -> dict[str, Any]:
        """Record an answer for the current section and attach its summary.

        This is two locked writes with the summarizer between them, not one.
        The summarizer can call a model, and the session lock must not be held
        across that. Both phases re-check the idempotency record, so a retry
        arriving while the summarizer is still running replays the first phase
        instead of appending a second response for the same answer.
        """

        exact = verbatim.strip()
        if not exact:
            raise WalkthruError("An answer cannot be empty; use --skip if desired.")
        recorded = self._record_verbatim_answer(walkthru_id, exact, operation_id=operation_id)
        if isinstance(recorded, dict):
            return recorded
        proposal = self._propose_section_summary(recorded, exact, summarize=summarize)
        return self._attach_summary_proposal(walkthru_id, proposal, operation_id=operation_id)

    def _record_verbatim_answer(
        self,
        walkthru_id: str,
        exact: str,
        *,
        operation_id: str,
    ) -> WalkthruSection | dict[str, Any]:
        """Persist the answer and advance to awaiting_summary.

        Returns the section the answer belongs to, or an already-completed
        result when this operation_id has been recorded before.
        """

        with self._lock_session(walkthru_id) as session:
            replay = self._load_idempotent_replay(session, operation_id)
            if replay is not None:
                return replay
            if session["state"] == "awaiting_answer":
                section = self._select_current_section(session)
                session["responses"].append(
                    {
                        "section_id": section.section_id,
                        "heading": section.heading,
                        "question": section.question,
                        "verbatim": exact,
                        "model_summary": None,
                        "model_suggestions": [],
                        "summary_method": None,
                        "accepted_summary": None,
                        "status": "awaiting_summary",
                    }
                )
                session["state"] = "awaiting_summary"
            elif session["state"] == "awaiting_summary":
                response = self._select_current_response(session)
                if response["verbatim"] != exact:
                    raise WalkthruError(
                        "A different answer is already saved and awaiting its summary."
                    )
                section = SECTION_BY_ID[response["section_id"]]
            else:
                self._require_state(session, "awaiting_answer")
        return section

    @staticmethod
    def _propose_section_summary(
        section: WalkthruSection,
        exact: str,
        *,
        summarize: Callable[[WalkthruSection, str], SummaryProposal],
    ) -> SummaryProposal:
        """Summarize outside the session lock, since this may call a model."""

        proposal = (
            SummaryProposal(summary=exact, method="verbatim_metadata")
            if section.section_id == "project"
            else summarize(section, exact)
        )
        if not proposal.summary.strip():
            return SummaryProposal(summary=exact, method="fallback_verbatim")
        return proposal

    def _attach_summary_proposal(
        self,
        walkthru_id: str,
        proposal: SummaryProposal,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        """Write the proposed summary and advance to awaiting_review."""

        with self._lock_session(walkthru_id) as session:
            replay = self._load_idempotent_replay(session, operation_id)
            if replay is not None:
                return replay
            self._require_state(session, "awaiting_summary")
            response = self._select_current_response(session)
            response["model_summary"] = proposal.summary.strip()
            response["model_suggestions"] = list(proposal.suggestions)
            response["summary_method"] = proposal.method
            response["status"] = "proposed"
            session["state"] = "awaiting_review"
            result = self._build_operation_result(session, status="summary_proposed")
            self._record_operation_result(session, operation_id, result)
            return result

    def accept_proposed_summary(
        self,
        walkthru_id: str,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        with self._lock_session(walkthru_id) as session:
            replay = self._load_idempotent_replay(session, operation_id)
            if replay is not None:
                return replay
            self._require_state(session, "awaiting_review")
            response = self._select_current_response(session)
            response["accepted_summary"] = response["model_summary"]
            response["status"] = "accepted"
            self._advance(session)
            result = self._build_operation_result(session, status="section_accepted")
            self._record_operation_result(session, operation_id, result)
            return result

    def revise_proposed_summary(
        self,
        walkthru_id: str,
        accepted_summary: str,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        with self._lock_session(walkthru_id) as session:
            replay = self._load_idempotent_replay(session, operation_id)
            if replay is not None:
                return replay
            self._require_state(session, "awaiting_review")
            corrected = accepted_summary.strip()
            if not corrected:
                raise WalkthruError("A revised summary cannot be empty.")
            response = self._select_current_response(session)
            response["accepted_summary"] = corrected
            response["status"] = "operator_revised"
            self._advance(session)
            result = self._build_operation_result(session, status="section_revised")
            self._record_operation_result(session, operation_id, result)
            return result

    def skip_section(
        self,
        walkthru_id: str,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        with self._lock_session(walkthru_id) as session:
            replay = self._load_idempotent_replay(session, operation_id)
            if replay is not None:
                return replay
            self._require_state(session, "awaiting_answer")
            section = self._select_current_section(session)
            session["responses"].append(
                {
                    "section_id": section.section_id,
                    "heading": section.heading,
                    "question": section.question,
                    "verbatim": None,
                    "model_summary": None,
                    "model_suggestions": [],
                    "summary_method": "skipped",
                    "accepted_summary": None,
                    "status": "skipped",
                }
            )
            self._advance(session)
            result = self._build_operation_result(session, status="section_skipped")
            self._record_operation_result(session, operation_id, result)
            return result

    def edit_accepted_summary(
        self,
        walkthru_id: str,
        section_id: str,
        accepted_summary: str,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        with self._lock_session(walkthru_id) as session:
            replay = self._load_idempotent_replay(session, operation_id)
            if replay is not None:
                return replay
            self._require_state(session, "ready_to_finish")
            if section_id not in SECTION_BY_ID:
                raise WalkthruError(f"Unknown walkthru section id: {section_id}")
            corrected = accepted_summary.strip()
            if not corrected:
                raise WalkthruError("An edited summary cannot be empty.")
            response = next(
                item for item in session["responses"] if item["section_id"] == section_id
            )
            response.setdefault("operator_summary_revisions", []).append(
                {"text": corrected, "created_at": _now()}
            )
            response["accepted_summary"] = corrected
            response["status"] = "operator_revised"
            result = self._build_operation_result(
                session,
                status="section_edited",
                include_responses=True,
            )
            self._record_operation_result(session, operation_id, result)
            return result

    def write_completed_sparse_gawd_draft(
        self,
        walkthru_id: str,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        with self._lock_session(walkthru_id) as session:
            replay = self._load_idempotent_replay(session, operation_id)
            if replay is not None:
                return replay
            self._require_state(session, "ready_to_finish")
            responses = {item["section_id"]: item for item in session["responses"]}
            project = str(responses.get("project", {}).get("accepted_summary") or "PROJECT NAME")
            section_bodies: dict[str, str] = {}
            for section_id, response in responses.items():
                heading = SECTION_BY_ID[section_id].heading
                accepted_summary = response.get("accepted_summary")
                if heading is not None and accepted_summary:
                    section_bodies[heading] = str(accepted_summary)
            draft_path = Path(session["draft_path"])
            created_at = datetime.fromisoformat(session["created_at"])
            markdown = render_sparse_gawd_draft(
                draft_id=session["draft_id"],
                created_at=created_at,
                project=project,
                section_bodies=section_bodies,
            )
            _atomic_write_text(draft_path, markdown)
            session["state"] = "finished"
            session["finished_at"] = _now()
            result = self._build_operation_result(
                session,
                status="walkthru_finished",
                include_responses=True,
            )
            self._record_operation_result(session, operation_id, result)
            return result

    def _build_operation_result(
        self,
        session: Mapping[str, Any],
        *,
        status: str,
        include_responses: bool = False,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "walkthru_id": session["walkthru_id"],
            "state": session["state"],
            "draft_id": session["draft_id"],
            "draft_path": session["draft_path"],
            "target_project_id": session.get("target_project_id"),
            "create_target_id": session.get("create_target_id"),
            "completed_sections": len(session["responses"]),
            "total_sections": len(SECTIONS),
            "execution_started": False,
        }
        if include_responses:
            result["responses"] = session["responses"]
        if session["state"] == "awaiting_answer":
            section = self._select_current_section(session)
            result["section"] = self._build_section_payload(section)
            result["next_commands"] = {
                "answer": self._build_resume_command(session, '--answer "YOUR ANSWER"'),
                "skip": self._build_resume_command(session, "--skip"),
            }
        elif session["state"] == "awaiting_summary":
            response = self._select_current_response(session)
            result["pending_answer"] = {
                "section_id": response["section_id"],
                "verbatim": response["verbatim"],
            }
        elif session["state"] == "awaiting_review":
            response = self._select_current_response(session)
            result["proposal"] = {
                "section_id": response["section_id"],
                "verbatim": response["verbatim"],
                "summary": response["model_summary"],
                "suggestions": response["model_suggestions"],
                "summary_method": response["summary_method"],
            }
            result["next_commands"] = {
                "accept": self._build_resume_command(session, "--accept"),
                "revise": self._build_resume_command(session, '--revise "CORRECTED SUMMARY"'),
            }
        elif session["state"] == "ready_to_finish":
            result["review"] = session["responses"]
            result["next_commands"] = {
                "edit": self._build_resume_command(
                    session,
                    '--edit SECTION_ID "CORRECTED SUMMARY"',
                ),
                "finish": self._build_resume_command(session, "--finish"),
            }
        elif session["state"] == "finished":
            result["draft_content"] = Path(str(session["draft_path"])).read_text(encoding="utf-8")
            next_command = f"pi /start /new-project {session['draft_path']}"
            if session.get("target_project_id"):
                next_command += f" --target-project-id {session['target_project_id']}"
            elif session.get("create_target_id"):
                next_command += f" --create-target {session['create_target_id']}"
            result["next_command"] = next_command
        return result

    @staticmethod
    def _build_section_payload(section: WalkthruSection) -> dict[str, str | None]:
        return {
            "section_id": section.section_id,
            "heading": section.heading,
            "label": section.label,
            "question": section.question,
            "guidance": section.guidance,
        }

    @staticmethod
    def _build_resume_command(session: Mapping[str, Any], suffix: str) -> str:
        return f"pi /start /new-project --walkthru {session['walkthru_id']} {suffix}"

    @staticmethod
    def _select_current_section(session: Mapping[str, Any]) -> WalkthruSection:
        index = int(session["current_section_index"])
        if not 0 <= index < len(SECTIONS):
            raise WalkthruError("The walkthru has no current section.")
        return SECTIONS[index]

    @staticmethod
    def _select_current_response(session: Mapping[str, Any]) -> dict[str, Any]:
        responses = session["responses"]
        if not responses:
            raise WalkthruError("The walkthru has no answer awaiting review.")
        return responses[-1]

    @staticmethod
    def _require_state(session: Mapping[str, Any], expected: WalkthruState) -> None:
        actual = session["state"]
        if actual != expected:
            raise WalkthruError(
                f"Walkthru is {actual}; this action requires {expected}. "
                "Run --status to inspect the next valid action."
            )

    @staticmethod
    def _advance(session: dict[str, Any]) -> None:
        next_index = int(session["current_section_index"]) + 1
        session["current_section_index"] = next_index
        session["state"] = "ready_to_finish" if next_index == len(SECTIONS) else "awaiting_answer"

    @staticmethod
    def _load_idempotent_replay(
        session: Mapping[str, Any], operation_id: str
    ) -> dict[str, Any] | None:
        if session.get("last_operation_id") != operation_id:
            return None
        result = session.get("last_result")
        return dict(result) if isinstance(result, dict) else None

    def _record_operation_result(
        self,
        session: dict[str, Any],
        operation_id: str,
        result: dict[str, Any],
    ) -> None:
        session["updated_at"] = _now()
        session["last_operation_id"] = operation_id
        session["last_result"] = result

    def _record_result_and_write_session(
        self,
        session: dict[str, Any],
        operation_id: str,
        result: dict[str, Any],
    ) -> None:
        self._record_operation_result(session, operation_id, result)
        self._write_session_file(self._build_session_path(session["walkthru_id"]), session)

    def _build_session_path(self, walkthru_id: str) -> Path:
        if not re.fullmatch(r"gawd-walkthru-[a-f0-9]{12}", walkthru_id):
            raise WalkthruError("Invalid walkthru id.")
        return self.directory / f"{walkthru_id}.json"

    @contextmanager
    def _lock_session(self, walkthru_id: str) -> Iterator[dict[str, Any]]:
        path = self._build_session_path(walkthru_id)
        lock_path = path.with_suffix(".lock")
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            session = self._read_session_file(path)
            try:
                yield session
            except Exception:
                raise
            else:
                self._write_session_file(path, session)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_session_file(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise WalkthruError(f"Walkthru not found: {path.stem}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise WalkthruError("Unsupported walkthru schema version.")
        _validate_session(payload)
        return payload

    @staticmethod
    def _write_session_file(path: Path, payload: Mapping[str, Any]) -> None:
        _validate_session(payload)
        _atomic_write_text(
            path,
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )


def parse_summary_proposal(output: Any, *, verbatim: str) -> SummaryProposal:
    candidate = output.get("text", output) if isinstance(output, dict) else output
    if isinstance(candidate, dict):
        return _parse_summary_proposal(candidate, verbatim=verbatim)
    if isinstance(candidate, str):
        raw = candidate.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
        if fenced:
            raw = fenced.group(1)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return SummaryProposal(summary=verbatim, method="fallback_verbatim")
        if isinstance(payload, dict):
            return _parse_summary_proposal(payload, verbatim=verbatim)
    return SummaryProposal(summary=verbatim, method="fallback_verbatim")


def summary_prompt(section: WalkthruSection, verbatim: str) -> str:
    return "\n".join(
        (
            "You are compiling one operator answer into a sparse GAWD project contract.",
            "Return only JSON with keys summary (string) and suggestions (array of strings).",
            (
                "The summary must be faithful: preserve uncertainty, exclusions, names, "
                "and approval gates."
            ),
            "Do not add requirements, milestones, facts, or decisions the operator did not state.",
            (
                "Put helpful inferences, missing details, or possible improvements only "
                "in suggestions."
            ),
            f"Section: {section.label}",
            f"Question: {section.question}",
            "Verbatim operator answer:",
            verbatim,
        )
    )


def _parse_summary_proposal(payload: Mapping[str, Any], *, verbatim: str) -> SummaryProposal:
    summary = payload.get("summary")
    suggestions = payload.get("suggestions")
    if not isinstance(summary, str) or not summary.strip():
        return SummaryProposal(summary=verbatim, method="fallback_verbatim")
    return SummaryProposal(
        summary=summary.strip(),
        suggestions=tuple(
            item.strip() for item in suggestions if isinstance(item, str) and item.strip()
        )
        if isinstance(suggestions, list)
        else (),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_session(session: Mapping[str, Any]) -> None:
    state = session.get("state")
    if state not in {
        "awaiting_answer",
        "awaiting_summary",
        "awaiting_review",
        "ready_to_finish",
        "finished",
    }:
        raise WalkthruError(f"Invalid persisted walkthru state: {state!r}")
    index = session.get("current_section_index")
    responses = session.get("responses")
    if not isinstance(index, int) or not isinstance(responses, list):
        raise WalkthruError("Walkthru index and responses have invalid persisted types.")
    expected_response_count = index + (1 if state in {"awaiting_summary", "awaiting_review"} else 0)
    if len(responses) != expected_response_count:
        raise WalkthruError(
            "Walkthru response count does not match its persisted state and section index."
        )
    if not 0 <= index <= len(SECTIONS):
        raise WalkthruError("Walkthru section index is outside the contract.")
    if state in {"ready_to_finish", "finished"} and index != len(SECTIONS):
        raise WalkthruError(f"Walkthru state {state} requires every section to be reviewed.")
    if state in {"awaiting_answer", "awaiting_summary", "awaiting_review"} and index >= len(
        SECTIONS
    ):
        raise WalkthruError(f"Walkthru state {state} requires a current section.")
    for ordinal, response in enumerate(responses):
        if (
            not isinstance(response, dict)
            or response.get("section_id") != SECTIONS[ordinal].section_id
        ):
            raise WalkthruError("Walkthru responses are not in contract section order.")
        response_status = response.get("status")
        if ordinal == index and state == "awaiting_summary":
            expected_status = "awaiting_summary"
        elif ordinal == index and state == "awaiting_review":
            expected_status = "proposed"
        else:
            expected_status = None
        if expected_status is not None and response_status != expected_status:
            raise WalkthruError("The current walkthru proposal has an invalid status.")
        if expected_status is None and response_status not in {
            "accepted",
            "operator_revised",
            "skipped",
        }:
            raise WalkthruError("A completed walkthru response has an invalid status.")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
