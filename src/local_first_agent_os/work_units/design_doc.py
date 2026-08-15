# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Loss-preserving DesignDoc parser.

Parsing is deliberately separate from compilation. This module answers "what does
the document say, and where does it say it", and nothing else: it never applies
policy, never invents a milestone, and never resolves a dependency. Everything it
returns carries a source span, so a later compiler diagnostic can point at the
exact characters an author wrote.

Unrecognized sections are preserved rather than dropped. A document written
against a newer template still round-trips through an older runtime, and an
author who invents a section heading gets it back in the structured content
instead of silently losing it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from ..ids import sha256_text
from .lifecycle import LifecyclePhase
from .permissions import PermissionAction
from .plan import DeliveryPace

SCHEMA_VERSION_PARSED_DESIGN_DOC = "parsed_design_doc.v1"


class DiagnosticSeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class SectionKind(StrEnum):
    """Which known role a section plays, or that it has none.

    ``UNKNOWN`` is a first-class member rather than an absence, because the
    forward-compatibility rule is that an unrecognized section is retained and
    named, not discarded.
    """

    REQUIREMENTS = "REQUIREMENTS"
    NON_GOALS = "NON_GOALS"
    CONSTRAINTS = "CONSTRAINTS"
    ASSUMPTIONS = "ASSUMPTIONS"
    ACCEPTANCE_CRITERIA = "ACCEPTANCE_CRITERIA"
    FAILURE_MODES = "FAILURE_MODES"
    ROLLOUT = "ROLLOUT"
    UNRESOLVED_QUESTIONS = "UNRESOLVED_QUESTIONS"
    APPROVALS = "APPROVALS"
    PERMISSION_ENVELOPE = "PERMISSION_ENVELOPE"
    REQUIRED_ARTIFACTS = "REQUIRED_ARTIFACTS"
    MILESTONES = "MILESTONES"
    MOTIVATION = "MOTIVATION"
    """Why the work is worth doing, as distinct from what it must achieve.

    Its own kind because the alternatives are both wrong in a way that costs
    something. `REQUIREMENTS` makes a rationale read as an obligation, and
    `ACCEPTANCE_CRITERIA` makes it read as a completion test, so an agent that
    cannot satisfy "the current process is manual" is left unable to finish.
    Motivation binds nothing; it explains why the bindings are worth honoring.
    """

    SCOPE = "SCOPE"
    """A section stating both what is in scope and what was cut.

    Its own kind because neither `REQUIREMENTS` nor `NON_GOALS` is true of the
    whole section, and picking either one publishes the other half under the
    wrong label: routing a GAWD doc's "Scope & Non-Goals" to `NON_GOALS` put its
    in-scope work under "Explicitly out of scope" in the executing agent's
    prompt, which is worse than not carrying it at all.
    """

    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SourceSpan:
    """Half-open character range in the original document text."""

    start: int
    end: int

    def to_payload(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class Diagnostic:
    """One parser or compiler finding, always anchored to the source."""

    severity: DiagnosticSeverity
    code: str
    message: str
    span: SourceSpan | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "span": self.span.to_payload() if self.span is not None else None,
        }


@dataclass(frozen=True)
class DocumentSection:
    heading: str
    normalized_heading: str
    level: int
    kind: SectionKind
    body: str
    span: SourceSpan

    def to_payload(self) -> dict[str, Any]:
        return {
            "heading": self.heading,
            "normalized_heading": self.normalized_heading,
            "level": self.level,
            "kind": self.kind.value,
            "body": self.body,
            "span": self.span.to_payload(),
        }


@dataclass(frozen=True)
class DeclaredPhase:
    """A milestone's phase together with how it was determined.

    An explicit declaration and a model's guess must never be indistinguishable
    downstream, so ``inferred`` travels with the value rather than being tracked
    somewhere else. Confidence and reasoning stay here, outside the compiled plan,
    because they are evidence about a classification and not authority.
    """

    phase: LifecyclePhase
    inferred: bool
    confidence: float | None = None
    reasoning: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "inferred": self.inferred,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }


@dataclass(frozen=True)
class MilestoneCandidate:
    """One milestone as written, before any policy is applied.

    A candidate is not executable. The compiler decides whether it becomes a
    compiled milestone, and the compiler is the only thing allowed to.
    """

    declared_key: str
    title: str
    description: str
    declared_phase: DeclaredPhase | None
    dependencies: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    required_artifacts: tuple[str, ...]
    executor_kind: str | None
    requires_operator_approval: bool
    source_heading: str
    span: SourceSpan
    unknown_fields: Mapping[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "declared_key": self.declared_key,
            "title": self.title,
            "description": self.description,
            "declared_phase": (
                self.declared_phase.to_payload() if self.declared_phase is not None else None
            ),
            "dependencies": list(self.dependencies),
            "acceptance_criteria": list(self.acceptance_criteria),
            "required_artifacts": list(self.required_artifacts),
            "executor_kind": self.executor_kind,
            "requires_operator_approval": self.requires_operator_approval,
            "source_heading": self.source_heading,
            "span": self.span.to_payload(),
            "unknown_fields": dict(sorted(self.unknown_fields.items())),
        }


@dataclass(frozen=True)
class DesignDocIdentity:
    design_doc_id: str
    title: str
    schema_version: str

    def to_payload(self) -> dict[str, str]:
        return {
            "design_doc_id": self.design_doc_id,
            "title": self.title,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class PermissionRequestSource:
    """One action that becomes available only when this exact plan is started."""

    action: PermissionAction
    reason: str

    def to_payload(self) -> dict[str, str]:
        return {"action": self.action.value, "reason": self.reason}


@dataclass(frozen=True)
class ParsedPermissionEnvelope:
    """The three permission states the author can express.

    There is deliberately no bare set of strings.  An action is autonomous,
    requested, or denied without a grant, and a parser cannot create a fourth
    half-state for downstream code to reinterpret.
    """

    autonomous: tuple[PermissionAction, ...]
    requested: tuple[PermissionRequestSource, ...]
    denied_without_approval: tuple[PermissionAction, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "autonomous": [item.value for item in self.autonomous],
            "requested": [item.to_payload() for item in self.requested],
            "denied_without_approval": [item.value for item in self.denied_without_approval],
        }


@dataclass(frozen=True)
class ParsedDesignDoc:
    """The whole document, structurally described and still fully present.

    ``raw_content`` is retained verbatim. The parsed form is an index into it,
    never a replacement for it: a compiled plan can be regenerated from the
    source, but a source cannot be regenerated from a summary.
    """

    schema_version: str
    identity: DesignDocIdentity
    raw_content: str
    content_hash: str
    source_path: str | None
    sections: tuple[DocumentSection, ...]
    motivation: tuple[str, ...]
    requirements: tuple[str, ...]
    non_goals: tuple[str, ...]
    constraints: tuple[str, ...]
    assumptions: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    failure_modes: tuple[str, ...]
    rollout: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    approval_requirements: tuple[str, ...]
    required_artifacts: tuple[str, ...]
    milestone_candidates: tuple[MilestoneCandidate, ...]
    diagnostics: tuple[Diagnostic, ...]
    permission_envelope: ParsedPermissionEnvelope | None = None
    declared_target_project_id: str | None = None
    """The registered project this document says the work is about, if it says.

    ``None`` means the document was silent, which the compiler resolves to the
    project-center default. It does not mean "no project": a WorkUnit always
    targets something, and a document that declines to choose gets the default
    rather than an absence the executor would have to invent a meaning for.
    """

    declared_delivery_pace: DeliveryPace = DeliveryPace.UNSPECIFIED
    """How wide the document asked to run, if it asked.

    ``UNSPECIFIED`` rather than ``None`` because silence is a pace: it means the
    plan runs at the authority ceiling. Modelling it as an absence would make
    every reader decide for itself what an absent pace means, and they would not
    all decide the same thing.
    """

    @property
    def unknown_sections(self) -> tuple[DocumentSection, ...]:
        return tuple(item for item in self.sections if item.kind is SectionKind.UNKNOWN)

    @property
    def has_errors(self) -> bool:
        return any(item.severity is DiagnosticSeverity.ERROR for item in self.diagnostics)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity.to_payload(),
            "content_hash": self.content_hash,
            "source_path": self.source_path,
            "sections": [item.to_payload() for item in self.sections],
            "motivation": list(self.motivation),
            "requirements": list(self.requirements),
            "non_goals": list(self.non_goals),
            "constraints": list(self.constraints),
            "assumptions": list(self.assumptions),
            "acceptance_criteria": list(self.acceptance_criteria),
            "failure_modes": list(self.failure_modes),
            "rollout": list(self.rollout),
            "unresolved_questions": list(self.unresolved_questions),
            "approval_requirements": list(self.approval_requirements),
            "required_artifacts": list(self.required_artifacts),
            "milestone_candidates": [item.to_payload() for item in self.milestone_candidates],
            "diagnostics": [item.to_payload() for item in self.diagnostics],
            "permission_envelope": (
                self.permission_envelope.to_payload()
                if self.permission_envelope is not None
                else None
            ),
            "declared_delivery_pace": self.declared_delivery_pace.value,
        }


# --------------------------------------------------------------------------- #
# Section recognition
# --------------------------------------------------------------------------- #

# Substring probes against the normalized heading. Order matters: the first
# match wins, so the more specific probe is listed first.
_SECTION_PROBES: tuple[tuple[str, SectionKind], ...] = (
    ("execution milestones", SectionKind.MILESTONES),
    ("milestones", SectionKind.MILESTONES),
    ("acceptance criteria", SectionKind.ACCEPTANCE_CRITERIA),
    # Both halves named in one heading, so more specific than either probe below
    # it and listed first. `_split_scope_section` decides which lines are which.
    # Probes are matched against `normalize_heading` output, which lowercases,
    # expands "&" to "and", and turns punctuation into spaces: the heading
    # "4. This Version - Scope & Non-Goals" arrives as
    # "this version scope and non goals", so the probe carries no punctuation.
    ("scope and non goals", SectionKind.SCOPE),
    ("non goals", SectionKind.NON_GOALS),
    ("requirements", SectionKind.REQUIREMENTS),
    ("constraints", SectionKind.CONSTRAINTS),
    ("assumptions", SectionKind.ASSUMPTIONS),
    ("failure modes", SectionKind.FAILURE_MODES),
    ("the failure that matters most", SectionKind.FAILURE_MODES),
    ("rollout", SectionKind.ROLLOUT),
    ("rollback", SectionKind.ROLLOUT),
    ("unresolved questions", SectionKind.UNRESOLVED_QUESTIONS),
    ("open questions", SectionKind.UNRESOLVED_QUESTIONS),
    ("approvals", SectionKind.APPROVALS),
    ("approval requirements", SectionKind.APPROVALS),
    ("required artifacts", SectionKind.REQUIRED_ARTIFACTS),
    # The GAWD doc's own section names. "the failure that matters most" above was
    # already one of them, which is the tell: this parser was always meant to read
    # a GAWD doc, and the mapping was simply left unfinished. A DesignDoc and a
    # GAWD doc are the same document; only the milestone blocks need typed fields,
    # because a phase and its evidence cannot be recovered from prose.
    ("risk synthesis", SectionKind.FAILURE_MODES),
    ("known limitations", SectionKind.FAILURE_MODES),
    ("permission envelope", SectionKind.PERMISSION_ENVELOPE),
    ("operational contract", SectionKind.CONSTRAINTS),
    # Before the bare "verification" probe, which would otherwise swallow it:
    # a current-state audit is what the document assumes to be true today, not
    # what it promises to prove.
    ("current state verification", SectionKind.ASSUMPTIONS),
    ("verification", SectionKind.ACCEPTANCE_CRITERIA),
    ("scope", SectionKind.REQUIREMENTS),
    # The golden flow is a statement of what must be true when the work is done,
    # which is what an acceptance criterion is. It reads as prose rather than
    # bullets, and that is fine: `_bullet_lines` keeps any non-empty line, and
    # this repository writes one sentence per line.
    ("happy path", SectionKind.ACCEPTANCE_CRITERIA),
    ("golden flow", SectionKind.ACCEPTANCE_CRITERIA),
    # Core design binds how the work may be built, which is what the compiler
    # already carries as global constraints on every milestone.
    ("core design", SectionKind.CONSTRAINTS),
    ("decision log", SectionKind.ASSUMPTIONS),
    # The last three GAWD sections that still landed in `UNKNOWN`. An unmapped
    # section is preserved but reaches no collection, so the document said these
    # and the agent executing it never heard them.
    #
    # The theory of the system is what the author holds true about the domain
    # before any work starts, which is an assumption in the only sense the
    # compiler uses the word.
    ("theory of the system", SectionKind.ASSUMPTIONS),
    # Deferred work is a non-goal that names its own reason for being deferred.
    ("if i had 2 more weeks", SectionKind.NON_GOALS),
    # Not `REQUIREMENTS` and not `ACCEPTANCE_CRITERIA`: see `SectionKind.MOTIVATION`.
    ("why this exists", SectionKind.MOTIVATION),
)

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<heading>.+?)\s*$", re.MULTILINE)
# The word "milestone" is required, not optional. Without it the pattern matches
# any numbered heading, so an ordinary document's `## 1. Theory of the System`
# silently becomes an executable milestone named "1"; a GAWD doc produced
# fourteen of them and then failed with fourteen `missing_phase` errors about
# sections that were never milestones. Requiring the marker means a document says
# what is executable rather than having it inferred from punctuation, and it is
# what lets a GAWD doc carry `### Milestone 3: ...` blocks under its own
# `## 8. Execution Milestones` heading without every other section joining in.
_MILESTONE_HEADING_RE = re.compile(
    r"^#{2,6}\s+milestone\s+(?P<key>[A-Za-z0-9][A-Za-z0-9._-]*)\s*[:.—-]\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
_FIELD_RE = re.compile(
    r"^[-*]?\s*(?:\*\*)?(?P<name>[A-Za-z][A-Za-z _-]*?)(?:\*\*)?\s*:\s*(?P<value>.*)$"
)

_MILESTONE_FIELD_ALIASES: Mapping[str, str] = {
    "phase": "phase",
    "lifecycle phase": "phase",
    "depends on": "dependencies",
    "dependencies": "dependencies",
    "acceptance": "acceptance_criteria",
    "acceptance criteria": "acceptance_criteria",
    "artifacts": "required_artifacts",
    "required artifacts": "required_artifacts",
    "evidence": "required_artifacts",
    "executor": "executor_kind",
    "executor kind": "executor_kind",
    "approval": "approval",
    "operator approval": "approval",
    "description": "description",
    "summary": "description",
}


def normalize_heading(heading: str) -> str:
    cleaned = re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", heading.strip().lower())
    cleaned = cleaned.replace("&", "and")
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    return cleaned.strip()


def normalize_milestone_key(raw: str) -> str:
    """Fold an authored identifier into the stable key vocabulary.

    Authors write ``A``, ``m1``, ``M-01``, or ``milestone_a``. All of those are
    one identity, so normalization happens once, here, and every later comparison
    is between normalized keys.
    """

    cleaned = raw.strip().lower()
    cleaned = re.sub(r"^milestone[\s_-]*", "", cleaned)
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    return cleaned


def _milestone_heading_match(level: int, heading: str) -> re.Match[str] | None:
    """Whether this heading declares a milestone, and its captured parts.

    One function answers it for both the section classifier and the milestone
    extractor, so a heading cannot be a milestone to one and an unknown section to
    the other.
    """

    return _MILESTONE_HEADING_RE.match(f"{'#' * level} {heading}")


def _section_kind(level: int, heading: str, normalized_heading: str) -> SectionKind:
    if _milestone_heading_match(level, heading) is not None:
        return SectionKind.MILESTONES
    for probe, kind in _SECTION_PROBES:
        if probe in normalized_heading:
            return kind
    return SectionKind.UNKNOWN


def _bullet_lines(body: str) -> tuple[str, ...]:
    out: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        cleaned = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line).strip()
        if cleaned:
            out.append(cleaned)
    return tuple(out)


def _criterion_lines(value: str) -> tuple[str, ...]:
    """One acceptance criterion, or nothing when the field was left empty."""

    criterion = value.strip().strip("`").strip()
    if not criterion or criterion.lower() in {"none", "n/a", "-"}:
        return ()
    return (criterion,)


def _split_list(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    parts = [part.strip().strip("`") for part in re.split(r"[,;]", value)]
    return tuple(part for part in parts if part and part.lower() not in {"none", "n/a", "-"})


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"yes", "true", "required", "y", "1"}


def _parse_phase(value: str) -> LifecyclePhase | None:
    candidate = value.strip().strip("`").upper()
    try:
        return LifecyclePhase(candidate)
    except ValueError:
        return None


def _split_sections(text: str) -> tuple[DocumentSection, ...]:
    matches = list(_HEADING_RE.finditer(text))
    sections: list[DocumentSection] = []
    for index, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        heading = match.group("heading").strip()
        normalized = normalize_heading(heading)
        level = len(match.group("hashes"))
        sections.append(
            DocumentSection(
                heading=heading,
                normalized_heading=normalized,
                level=level,
                kind=_section_kind(level, heading, normalized),
                body=text[body_start:body_end],
                span=SourceSpan(match.start(), body_end),
            )
        )
    return tuple(sections)


def _collect(sections: Sequence[DocumentSection], kind: SectionKind) -> tuple[str, ...]:
    out: list[str] = []
    for section in sections:
        if section.kind is kind:
            out.extend(_bullet_lines(section.body))
        elif section.kind is SectionKind.SCOPE:
            in_scope, cut = _split_scope_section(section.body)
            if kind is SectionKind.REQUIREMENTS:
                out.extend(in_scope)
            elif kind is SectionKind.NON_GOALS:
                out.extend(cut)
    return tuple(out)


_PERMISSION_CATEGORY_LABELS: Final = {
    "autonomous permissions": "autonomous",
    "requested permissions": "requested",
    "denied without explicit approval": "denied",
    "denied without approval": "denied",
    "risks": "risks",
}


def _parse_permission_envelope(
    sections: Sequence[DocumentSection],
) -> tuple[ParsedPermissionEnvelope | None, tuple[Diagnostic, ...]]:
    """Parse the generated envelope without granting unknown prose.

    The labels are part of the authoring contract.  A misspelled action is an
    error rather than a string downstream code can silently ignore, because
    ignoring a denial and ignoring a grant are both authority changes.
    """

    matching = [item for item in sections if item.kind is SectionKind.PERMISSION_ENVELOPE]
    if not matching:
        return None, ()
    if len(matching) > 1:
        return None, (
            Diagnostic(
                DiagnosticSeverity.ERROR,
                "duplicate_permission_envelope",
                "the document declares more than one Permission Envelope",
                matching[1].span,
            ),
        )

    section = matching[0]
    autonomous: list[PermissionAction] = []
    requested: list[PermissionRequestSource] = []
    denied: list[PermissionAction] = []
    diagnostics: list[Diagnostic] = []
    category: str | None = None
    for raw_line in section.body.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        unmarked = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", stripped).strip()
        label = normalize_heading(unmarked.rstrip(":"))
        if label in _PERMISSION_CATEGORY_LABELS and not stripped.startswith(("-", "*", "+")):
            category = _PERMISSION_CATEGORY_LABELS[label]
            continue
        if category in {None, "risks"}:
            continue
        action_name, separator, reason = unmarked.partition(":")
        try:
            action = PermissionAction(action_name.strip())
        except ValueError:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "unknown_permission_action",
                    f"the Permission Envelope names unknown action {action_name.strip()!r}",
                    section.span,
                )
            )
            continue
        if category == "autonomous":
            autonomous.append(action)
        elif category == "requested":
            requested.append(
                PermissionRequestSource(
                    action=action,
                    reason=reason.strip() if separator else "",
                )
            )
        elif category == "denied":
            denied.append(action)

    autonomous_set = set(autonomous)
    requested_set = {item.action for item in requested}
    denied_set = set(denied)
    conflicts = autonomous_set & (requested_set | denied_set)
    if conflicts:
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.ERROR,
                "conflicting_permission_action",
                "the Permission Envelope gives autonomous and gated meanings to: "
                + ", ".join(sorted(item.value for item in conflicts)),
                section.span,
            )
        )

    return (
        ParsedPermissionEnvelope(
            autonomous=tuple(dict.fromkeys(autonomous)),
            requested=tuple(dict.fromkeys(requested)),
            denied_without_approval=tuple(dict.fromkeys(denied)),
        ),
        tuple(diagnostics),
    )


_CUT_MARKER_RE = re.compile(r"^\**\s*(?:cut|out of scope|non[- ]goals?|not doing)\b", re.IGNORECASE)
_IN_SCOPE_MARKER_RE = re.compile(r"^\**\s*(?:in ?scope|scope)\b", re.IGNORECASE)


def _split_scope_section(body: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Divide a combined scope section into what is in and what was cut.

    A GAWD doc writes both under one heading, marked by bold sub-labels
    ("**In scope.**", "**Cut (non-goals).**"). Routing the whole section to
    either kind mislabels the other half, and mislabelled scope is worse than
    absent scope: an agent told its work is out of scope will not do it.

    Lines before any marker count as in scope, because a section that lists work
    and only later says what was cut is describing this version until it says
    otherwise. The markers themselves are dropped; they are labels, not content.
    """

    in_scope: list[str] = []
    cut: list[str] = []
    target = in_scope
    for line in _bullet_lines(body):
        if _CUT_MARKER_RE.match(line):
            target = cut
            continue
        if _IN_SCOPE_MARKER_RE.match(line):
            target = in_scope
            continue
        target.append(line)
    return tuple(in_scope), tuple(cut)


def _document_title(text: str, sections: Sequence[DocumentSection]) -> str:
    for section in sections:
        if section.level == 1:
            return section.heading
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first or "untitled design doc"


_AMBIGUOUS_TARGET_PROJECT_FIELD_NAME: Final = "project"
_TARGET_PROJECT_FIELD_NAMES: Final = (
    "target project",
    "target_project",
    _AMBIGUOUS_TARGET_PROJECT_FIELD_NAME,
)


def _declared_target_project_id(text: str, sections: Sequence[DocumentSection]) -> str | None:
    """Read a document-level ``Target project:`` line from the preamble.

    Only the preamble, meaning everything before the first ``##`` section. A
    document that names a project inside a milestone block is talking about that
    milestone, and one WorkUnit targets one repository, so accepting it anywhere
    would let two milestones disagree about a plan-level fact.

    The boundary is the first level-2 heading rather than the first section: the
    title is a level-1 section starting at character zero, so measuring from any
    section would make the preamble empty and this field unreadable.
    """

    first_section_start = min(
        (section.span.start for section in sections if section.level >= 2),
        default=len(text),
    )
    fallback: str | None = None
    for line in text[:first_section_start].splitlines():
        match = _FIELD_RE.match(line.strip())
        if match is None:
            continue
        name = normalize_heading(match.group("name"))
        if name not in _TARGET_PROJECT_FIELD_NAMES:
            continue
        value = match.group("value").strip().strip("`") or None
        if name == _AMBIGUOUS_TARGET_PROJECT_FIELD_NAME:
            # `Project:` is a loose alias, and the GAWD template's banner line
            # `**Project:** X | **Version:** Y | ...` matches it. Keep the alias, so a
            # document that only says `Project:` still works, but never let it beat
            # an unambiguous `Target project:`. Before this, which one won was
            # decided by whichever appeared first in the file.
            fallback = fallback or _banner_project_id(value)
            continue
        return value
    return fallback


def _banner_project_id(value: str | None) -> str | None:
    """The project id out of a banner line, or out of a plain one unchanged.

    The template this repository ships writes its header as
    `**Project:** X | **Version:** Y | **Status:** Z`, so the loose alias used to
    hand back everything after `Project:` - emphasis marks, version, status and
    date - as the id. The compile then failed with `target project '** X |
    **Version:** ...' is not registered`, which names the symptom and not the
    cause, and the reader's next move is to guess.

    Reading the banner is the fix rather than telling authors to add a second
    line, because the banner is what the template generates: a rule that every
    document must avoid the format the system itself writes is a rule that will
    be broken by every document the system writes.
    """

    if value is None:
        return None
    return value.split("|", 1)[0].replace("*", "").strip().strip("`") or None


_DELIVERY_PACE_FIELD_NAMES: Final = ("pace", "delivery pace", "delivery_pace", "timeline")


def _declared_delivery_pace(
    text: str, sections: Sequence[DocumentSection]
) -> tuple[DeliveryPace, Diagnostic | None]:
    """Read a document-level ``Pace:`` line from the preamble.

    Preamble-only for the same reason as the target project: pace is a
    plan-level fact, and a milestone that declared its own would be describing a
    schedule the phase loop has no way to honour per milestone.

    An unrecognised value is a warning and ``UNSPECIFIED``, not an error. The
    value names a bound the runtime enforces anyway, so the worst case of
    misreading it is a plan that runs at the ceiling it would have run at had
    the line been absent. Failing compilation over it would reject a document
    for a typo in an optional field, which is a worse trade than saying so and
    scheduling conservatively.
    """

    first_section_start = min(
        (section.span.start for section in sections if section.level >= 2),
        default=len(text),
    )
    for line in text[:first_section_start].splitlines():
        match = _FIELD_RE.match(line.strip())
        if match is None:
            continue
        if normalize_heading(match.group("name")) not in _DELIVERY_PACE_FIELD_NAMES:
            continue
        raw = match.group("value").strip().strip("`")
        if not raw:
            return DeliveryPace.UNSPECIFIED, None
        try:
            return DeliveryPace(raw.strip().lower()), None
        except ValueError:
            accepted = ", ".join(sorted(item.value for item in DeliveryPace))
            return DeliveryPace.UNSPECIFIED, Diagnostic(
                severity=DiagnosticSeverity.WARNING,
                code="unknown_delivery_pace",
                message=(
                    f"unrecognised delivery pace {raw!r}; scheduling at the "
                    f"authority ceiling. Accepted values: {accepted}"
                ),
            )
    return DeliveryPace.UNSPECIFIED, None


@dataclass
class _CandidateFields:
    """Mutable accumulator for one milestone block's fields.

    Kept private and small: it exists only so field parsing can be written once
    for both the structured heading form and the flat bullet form.
    """

    description: str = ""
    phase: LifecyclePhase | None = None
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    executor_kind: str | None = None
    approval: bool = False
    unknown: dict[str, str] = field(default_factory=dict)
    phase_text: str | None = None


def _parse_candidate_fields(body: str) -> _CandidateFields:
    fields = _CandidateFields()
    description_lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _FIELD_RE.match(line)
        if match is None:
            description_lines.append(line)
            continue
        name = normalize_heading(match.group("name"))
        value = match.group("value").strip()
        canonical = _MILESTONE_FIELD_ALIASES.get(name)
        if canonical is None:
            fields.unknown[name] = value
            continue
        if canonical == "phase":
            fields.phase_text = value
            fields.phase = _parse_phase(value)
        elif canonical == "dependencies":
            fields.dependencies = _split_list(value)
        elif canonical == "acceptance_criteria":
            # One line is one criterion, and repeating the field adds another.
            # Splitting on commas the way a dependency list is split turned
            # "a plan names the caller, the site, and the capability" into three
            # criteria, two of which were sentence fragments an agent would then
            # be asked to satisfy. The numbered-list form at `_prose_candidates`
            # already keeps a whole item, and these two paths have to agree about
            # whether a criterion is prose.
            fields.acceptance_criteria = (*fields.acceptance_criteria, *_criterion_lines(value))
        elif canonical == "required_artifacts":
            fields.required_artifacts = _split_list(value)
        elif canonical == "executor_kind":
            fields.executor_kind = value.strip("`") or None
        elif canonical == "approval":
            fields.approval = _parse_bool(value)
        elif canonical == "description":
            description_lines.append(value)
    fields.description = " ".join(item for item in description_lines if item)
    return fields


def _milestone_blocks(
    sections: Sequence[DocumentSection],
) -> tuple[tuple[DocumentSection, re.Match[str]], ...]:
    blocks: list[tuple[DocumentSection, re.Match[str]]] = []
    for section in sections:
        heading_match = _milestone_heading_match(section.level, section.heading)
        if heading_match is not None:
            blocks.append((section, heading_match))
    return tuple(blocks)


_NUMBERED_ITEM_RE = re.compile(r"^(?P<key>\d+)[.)]\s+(?P<text>\S.*)$")


def _listed_milestone_candidates(
    sections: Sequence[DocumentSection],
) -> tuple[MilestoneCandidate, ...]:
    """Read milestones from a numbered list under a milestones section.

    A GAWD doc writes its execution milestones as an ordered list rather than as
    one heading each, and that list is already the thing it means: ordered,
    one item per durable transition. Reading it is deterministic, so it belongs
    here rather than behind a model.

    Three fields come out and the rest are deliberately left alone. The item text
    becomes the title, the description, and a single acceptance criterion, because
    a milestone written as one sentence has said its acceptance in that sentence
    and the compiler rejects a milestone with none. Required artifacts stay empty
    because the executor registry declares them and the compiler unions them in.
    The phase stays undeclared: it is the one field that cannot be recovered from
    prose, so it has to arrive as a declaration or as a marked inference rather
    than as a guess made here.

    Position implies order, not dependency. A numbered list looks sequential and
    usually is, but "usually" is not a thing to compile into a dependency edge
    that then blocks a phase, so dependencies stay empty too.
    """

    candidates: list[MilestoneCandidate] = []
    for section in sections:
        if section.kind is not SectionKind.MILESTONES:
            continue
        # A section that spells its milestones as headings has already been read
        # by `_milestone_blocks`; reading its body as well would double every one.
        if any(
            _milestone_heading_match(other.level, other.heading) is not None
            for other in sections
            if other.span.start >= section.span.start and other.span.end <= section.span.end
        ):
            continue
        offset = section.span.end - len(section.body)
        cursor = 0
        for raw_line in section.body.splitlines(keepends=True):
            line_start = offset + cursor
            cursor += len(raw_line)
            match = _NUMBERED_ITEM_RE.match(raw_line.strip())
            if match is None:
                continue
            text = match.group("text").strip()
            candidates.append(
                MilestoneCandidate(
                    declared_key=normalize_milestone_key(match.group("key")),
                    title=_first_clause(text),
                    description=text,
                    declared_phase=None,
                    dependencies=(),
                    acceptance_criteria=(text,),
                    required_artifacts=(),
                    executor_kind=None,
                    requires_operator_approval=False,
                    source_heading=section.heading,
                    span=SourceSpan(start=line_start, end=line_start + len(raw_line)),
                )
            )
    return tuple(candidates)


def _first_clause(text: str) -> str:
    """A short title from a one-sentence milestone.

    Cut at the first sentence or clause boundary so a title stays readable in a
    cockpit column, and fall back to the whole text when there is no boundary
    rather than truncating mid-word.
    """

    for separator in (";", ". ", ":"):
        head, found, _ = text.partition(separator)
        if found and head.strip():
            return head.strip()
    return text


def parse_design_doc(
    raw_content: str,
    *,
    design_doc_id: str,
    source_path: str | None = None,
) -> ParsedDesignDoc:
    """Parse a DesignDoc without applying any policy.

    Deterministic syntax is parsed first and completely. Nothing here calls a
    model: an inferred phase can only enter this representation through
    ``apply_phase_inference``, which marks it as inferred.
    """

    sections = _split_sections(raw_content)
    diagnostics: list[Diagnostic] = []
    candidates: list[MilestoneCandidate] = []
    seen_keys: dict[str, SourceSpan] = {}

    for section, heading_match in _milestone_blocks(sections):
        declared_key = normalize_milestone_key(heading_match.group("key"))
        title = heading_match.group("title").strip()
        fields = _parse_candidate_fields(section.body)
        if fields.phase_text is not None and fields.phase is None:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "unknown_phase",
                    (
                        f"milestone {declared_key!r} declares phase "
                        f"{fields.phase_text!r}, which is not a lifecycle phase"
                    ),
                    section.span,
                )
            )
        if not declared_key:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "malformed_milestone",
                    f"milestone heading {section.heading!r} has no usable identifier",
                    section.span,
                )
            )
            continue
        if declared_key in seen_keys:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.ERROR,
                    "duplicate_milestone_key",
                    (
                        f"milestone identifier {declared_key!r} is declared twice; "
                        f"first declaration at character {seen_keys[declared_key].start}"
                    ),
                    section.span,
                )
            )
            continue
        seen_keys[declared_key] = section.span
        declared_phase = (
            DeclaredPhase(phase=fields.phase, inferred=False) if fields.phase is not None else None
        )
        candidates.append(
            MilestoneCandidate(
                declared_key=declared_key,
                title=title,
                description=fields.description,
                declared_phase=declared_phase,
                dependencies=tuple(
                    normalize_milestone_key(item) for item in fields.dependencies if item
                ),
                acceptance_criteria=fields.acceptance_criteria,
                required_artifacts=fields.required_artifacts,
                executor_kind=fields.executor_kind,
                requires_operator_approval=fields.approval,
                source_heading=section.heading,
                span=section.span,
                unknown_fields=dict(fields.unknown),
            )
        )

    for listed in _listed_milestone_candidates(sections):
        # A heading always wins over a list item with the same key: the heading
        # form carries typed fields and the list form carries none, so preferring
        # the list would silently discard a declared phase.
        if listed.declared_key in seen_keys:
            continue
        seen_keys[listed.declared_key] = listed.span
        candidates.append(listed)

    for candidate in candidates:
        for dependency in candidate.dependencies:
            if dependency not in seen_keys:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticSeverity.ERROR,
                        "unresolved_dependency",
                        (
                            f"milestone {candidate.declared_key!r} depends on "
                            f"{dependency!r}, which no milestone declares"
                        ),
                        candidate.span,
                    )
                )

    if not candidates:
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.ERROR,
                "no_milestones",
                "the document declares no milestones, so it has no executable work",
                None,
            )
        )

    for section in sections:
        if section.kind is SectionKind.UNKNOWN and section.level >= 2:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.INFO,
                    "unknown_section_preserved",
                    f"section {section.heading!r} has no known role and was preserved verbatim",
                    section.span,
                )
            )

    delivery_pace, pace_diagnostic = _declared_delivery_pace(raw_content, sections)
    if pace_diagnostic is not None:
        diagnostics.append(pace_diagnostic)
    permission_envelope, permission_diagnostics = _parse_permission_envelope(sections)
    diagnostics.extend(permission_diagnostics)

    return ParsedDesignDoc(
        schema_version=SCHEMA_VERSION_PARSED_DESIGN_DOC,
        identity=DesignDocIdentity(
            design_doc_id=design_doc_id,
            title=_document_title(raw_content, sections),
            schema_version=SCHEMA_VERSION_PARSED_DESIGN_DOC,
        ),
        raw_content=raw_content,
        content_hash=sha256_text(raw_content),
        source_path=source_path,
        sections=sections,
        motivation=_collect(sections, SectionKind.MOTIVATION),
        requirements=_collect(sections, SectionKind.REQUIREMENTS),
        non_goals=_collect(sections, SectionKind.NON_GOALS),
        constraints=_collect(sections, SectionKind.CONSTRAINTS),
        assumptions=_collect(sections, SectionKind.ASSUMPTIONS),
        acceptance_criteria=_collect(sections, SectionKind.ACCEPTANCE_CRITERIA),
        failure_modes=_collect(sections, SectionKind.FAILURE_MODES),
        rollout=_collect(sections, SectionKind.ROLLOUT),
        unresolved_questions=_collect(sections, SectionKind.UNRESOLVED_QUESTIONS),
        declared_target_project_id=_declared_target_project_id(raw_content, sections),
        declared_delivery_pace=delivery_pace,
        approval_requirements=_collect(sections, SectionKind.APPROVALS),
        required_artifacts=_collect(sections, SectionKind.REQUIRED_ARTIFACTS),
        milestone_candidates=tuple(candidates),
        diagnostics=tuple(diagnostics),
        permission_envelope=permission_envelope,
    )


@dataclass(frozen=True)
class PhaseInference:
    """A proposed phase for a legacy milestone that declared none."""

    milestone_key: str
    phase: LifecyclePhase
    confidence: float
    reasoning: str


def apply_phase_inference(
    parsed: ParsedDesignDoc,
    inferences: Iterable[PhaseInference],
    *,
    confirmation_threshold: float = 0.9,
) -> ParsedDesignDoc:
    """Attach proposed phases to candidates that declared none.

    An inference never becomes indistinguishable from a declaration: the
    resulting ``DeclaredPhase`` carries ``inferred=True`` plus its confidence and
    reasoning, and anything below ``confirmation_threshold`` also emits a
    diagnostic that the compiler turns into an execution blocker. An unconfirmed
    inference therefore cannot start work, grant a permission, or skip a gate.
    """

    by_key = {item.milestone_key: item for item in inferences}
    diagnostics = list(parsed.diagnostics)
    updated: list[MilestoneCandidate] = []
    for candidate in parsed.milestone_candidates:
        inference = by_key.get(candidate.declared_key)
        if candidate.declared_phase is not None or inference is None:
            updated.append(candidate)
            continue
        updated.append(
            MilestoneCandidate(
                declared_key=candidate.declared_key,
                title=candidate.title,
                description=candidate.description,
                declared_phase=DeclaredPhase(
                    phase=inference.phase,
                    inferred=True,
                    confidence=inference.confidence,
                    reasoning=inference.reasoning,
                ),
                dependencies=candidate.dependencies,
                acceptance_criteria=candidate.acceptance_criteria,
                required_artifacts=candidate.required_artifacts,
                executor_kind=candidate.executor_kind,
                requires_operator_approval=candidate.requires_operator_approval,
                source_heading=candidate.source_heading,
                span=candidate.span,
                unknown_fields=candidate.unknown_fields,
            )
        )
        if inference.confidence < confirmation_threshold:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.WARNING,
                    "ambiguous_inferred_phase",
                    (
                        f"milestone {candidate.declared_key!r} has an inferred phase "
                        f"{inference.phase.value} at confidence {inference.confidence:.2f}; "
                        "an operator must confirm it before execution"
                    ),
                    candidate.span,
                )
            )
    return ParsedDesignDoc(
        schema_version=parsed.schema_version,
        identity=parsed.identity,
        raw_content=parsed.raw_content,
        content_hash=parsed.content_hash,
        source_path=parsed.source_path,
        sections=parsed.sections,
        motivation=parsed.motivation,
        requirements=parsed.requirements,
        non_goals=parsed.non_goals,
        constraints=parsed.constraints,
        assumptions=parsed.assumptions,
        acceptance_criteria=parsed.acceptance_criteria,
        failure_modes=parsed.failure_modes,
        rollout=parsed.rollout,
        unresolved_questions=parsed.unresolved_questions,
        approval_requirements=parsed.approval_requirements,
        required_artifacts=parsed.required_artifacts,
        declared_target_project_id=parsed.declared_target_project_id,
        declared_delivery_pace=parsed.declared_delivery_pace,
        milestone_candidates=tuple(updated),
        diagnostics=tuple(diagnostics),
        permission_envelope=parsed.permission_envelope,
    )


__all__ = [
    "SCHEMA_VERSION_PARSED_DESIGN_DOC",
    "DeclaredPhase",
    "DesignDocIdentity",
    "Diagnostic",
    "DiagnosticSeverity",
    "DocumentSection",
    "MilestoneCandidate",
    "ParsedPermissionEnvelope",
    "ParsedDesignDoc",
    "PermissionRequestSource",
    "PhaseInference",
    "SectionKind",
    "SourceSpan",
    "apply_phase_inference",
    "normalize_heading",
    "normalize_milestone_key",
    "parse_design_doc",
]
