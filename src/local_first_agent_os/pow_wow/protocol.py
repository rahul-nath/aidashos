# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed task-purpose and review-verdict protocol for pow-wow execution.

Model text is parsed once at this boundary. Executor control flow consumes the
enums below instead of repeatedly inspecting role names or verdict prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from ..coordination.contracts import DispatchKind


class TaskPurpose(StrEnum):
    """Semantic purpose of a pow-wow task."""

    ADVISORY = "advisory"
    IMPLEMENTATION = "implementation"
    RECOVERY_REVISION = "recovery_revision"
    REVIEW = "review"
    DETERMINISTIC_CHECK = "deterministic_check"
    BROWSER_ACCEPTANCE = "browser_acceptance"


class ReferencePack(StrEnum):
    """Bounded doctrine packs explicitly selected by a durable task."""

    MARKETING_SITE = "marketing_site"


class PlanningPhase(StrEnum):
    """Model-visibility phase for independent planning workflows.

    These values are persisted with task specifications. The executor validates
    their dependency graph before launching a model, so independent reading is
    an execution contract rather than an instruction hidden in prose.
    """

    SENIOR_INDEPENDENT_READING = "senior_independent_reading"
    JUNIOR_VERIFICATION_PLAN = "junior_verification_plan"
    SENIOR_OWNED_PLAN = "senior_owned_plan"
    STAFF_INDEPENDENT_READING = "staff_independent_reading"
    STAFF_FINAL_REVIEW = "staff_final_review"


class ReviewOrigin(StrEnum):
    """Host-stamped source of review evidence."""

    AUTOMATED_STAFF = "AUTOMATED_STAFF"
    RECOVERY_STAFF = "RECOVERY_STAFF"
    OPERATOR_EVIDENCE = "OPERATOR_EVIDENCE"


class ReviewerTier(StrEnum):
    """Finite reviewer identities; never accepted from model prose."""

    STAFF = "STAFF"
    SENIOR = "SENIOR"
    OPERATOR = "OPERATOR"


class ReviewFindingSeverity(StrEnum):
    BLOCKING = "BLOCKING"
    NON_BLOCKING = "NON_BLOCKING"
    UNKNOWN = "UNKNOWN"


class ReviewCompletionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReviewDisposition(StrEnum):
    """Finite control-flow outcomes parsed from a reviewer response."""

    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    REJECT = "reject"
    ESCALATE = "escalate"
    UNCLASSIFIED = "unclassified"

    @property
    def requests_changes(self) -> bool:
        return self in {
            ReviewDisposition.REQUEST_CHANGES,
            ReviewDisposition.REJECT,
        }


_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+")
_APPROVAL_TOKENS = frozenset({"approve", "approved", "accept", "accepted"})
_CHANGE_TOKENS = frozenset({"block", "blocked", "request_changes", "changes_requested"})
_REJECTION_TOKENS = frozenset({"reject", "rejected"})
_ESCALATION_TOKENS = frozenset({"escalate", "escalated"})
# A line that announces itself as the decision, wherever it sits in the text.
# Reviewers are told to open with the verdict, and on 2026-08-10 both frontier
# reviewers opened with a markdown heading instead and put "**Verdict:
# APPROVE.**" two lines down. First-line-only parsing read the title, classified
# both reviews UNCLASSIFIED, and failed two substantively approved dispatches
# closed. The label is the guard against the opposite failure: prose that merely
# mentions "approve" somewhere must never become an approval, so a non-first
# line counts only when it names itself a verdict or decision.
_LABELED_DECISION_PATTERN = re.compile(r"^\W*(?:verdict|decision)\b", re.IGNORECASE)


def _classify_tokens(line: str) -> ReviewDisposition:
    tokens = frozenset(_TOKEN_PATTERN.findall(line.casefold()))
    if tokens & _REJECTION_TOKENS:
        return ReviewDisposition.REJECT
    if tokens & _CHANGE_TOKENS:
        return ReviewDisposition.REQUEST_CHANGES
    if tokens & _ESCALATION_TOKENS:
        return ReviewDisposition.ESCALATE
    if tokens & _APPROVAL_TOKENS:
        return ReviewDisposition.APPROVE
    return ReviewDisposition.UNCLASSIFIED


@dataclass(frozen=True)
class ReviewVerdict:
    """Parsed reviewer output with the original text retained as evidence."""

    disposition: ReviewDisposition
    text: str
    decision_line: str | None

    @classmethod
    def parse(cls, text: str) -> ReviewVerdict:
        normalized = text.strip()
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        if not lines:
            return cls(ReviewDisposition.UNCLASSIFIED, normalized, None)
        # The demanded contract first: a verdict token on the opening line.
        disposition = _classify_tokens(lines[0])
        if disposition is not ReviewDisposition.UNCLASSIFIED:
            return cls(disposition, normalized, lines[0])
        # Then the observed reality: the first line that calls itself a verdict.
        # Unlabeled prose stays unparsed on purpose, and an unparsed review still
        # fails closed downstream.
        for line in lines[1:]:
            if not _LABELED_DECISION_PATTERN.match(line):
                continue
            disposition = _classify_tokens(line)
            if disposition is not ReviewDisposition.UNCLASSIFIED:
                return cls(disposition, normalized, line)
        return cls(ReviewDisposition.UNCLASSIFIED, normalized, lines[0])


def classify_finding_severity(disposition: ReviewDisposition) -> ReviewFindingSeverity:
    if disposition in {
        ReviewDisposition.REQUEST_CHANGES,
        ReviewDisposition.REJECT,
        ReviewDisposition.ESCALATE,
    }:
        return ReviewFindingSeverity.BLOCKING
    if disposition is ReviewDisposition.APPROVE:
        return ReviewFindingSeverity.NON_BLOCKING
    return ReviewFindingSeverity.UNKNOWN


def infer_legacy_task_purpose(
    *,
    task_name: str,
    role: str,
    judgment_name: str | None,
    dispatch_kind: DispatchKind | None,
) -> TaskPurpose:
    """Parse old string-shaped tasks once while persisted v1 payloads remain.

    Newly constructed tasks should pass ``purpose`` explicitly. This parser is
    the compatibility boundary for old fixtures and durable payloads.
    """

    if judgment_name == "reviewer":
        return TaskPurpose.REVIEW
    if judgment_name is not None:
        if dispatch_kind is DispatchKind.CODE:
            return TaskPurpose.IMPLEMENTATION
        return TaskPurpose.ADVISORY
    marker = f"{role} {task_name}".casefold()
    if "review" in marker:
        return TaskPurpose.REVIEW
    if dispatch_kind is DispatchKind.CODE or "implement" in marker:
        return TaskPurpose.IMPLEMENTATION
    return TaskPurpose.DETERMINISTIC_CHECK


__all__ = [
    "ReferencePack",
    "ReviewCompletionStatus",
    "ReviewDisposition",
    "ReviewFindingSeverity",
    "ReviewOrigin",
    "ReviewerTier",
    "PlanningPhase",
    "ReviewVerdict",
    "TaskPurpose",
    "classify_finding_severity",
    "infer_legacy_task_purpose",
]
