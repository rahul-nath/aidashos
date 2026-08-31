# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The parser reports what a document says, with source positions, and loses nothing.

Every assertion here is about honesty rather than convenience: an unknown section
survives, a duplicate identifier is reported instead of silently overwriting the
first one, and a missing phase is never guessed.
"""

from __future__ import annotations

from work_unit_support import ACCEPTANCE_DESIGN_DOC

from local_first_agent_os.work_units.design_doc import (
    DiagnosticSeverity,
    PhaseInference,
    SectionKind,
    apply_phase_inference,
    normalize_milestone_key,
    parse_design_doc,
)
from local_first_agent_os.work_units.lifecycle import LifecyclePhase


def test_parses_a_complete_design_doc_into_typed_sections_and_milestones() -> None:
    parsed = parse_design_doc(ACCEPTANCE_DESIGN_DOC, design_doc_id="doc")

    assert parsed.raw_content == ACCEPTANCE_DESIGN_DOC
    assert [item.declared_key for item in parsed.milestone_candidates] == [
        "a",
        "b",
        "c",
        "d",
        "e",
        "f",
    ]
    assert parsed.requirements == ("Compile one DesignDoc revision into one immutable plan.",)
    assert parsed.constraints == ("A document may not supply executable code.",)
    assert parsed.non_goals == ("A general workflow language.",)
    assert not parsed.has_errors


def test_preserves_the_exact_source_text_and_its_hash() -> None:
    parsed = parse_design_doc(ACCEPTANCE_DESIGN_DOC, design_doc_id="doc")

    # The parsed model is an index into the source, never a replacement for it.
    assert parsed.raw_content == ACCEPTANCE_DESIGN_DOC
    assert (
        parsed.content_hash
        == parse_design_doc(ACCEPTANCE_DESIGN_DOC, design_doc_id="other").content_hash
    )


def test_source_spans_locate_each_milestone_in_the_original_text() -> None:
    parsed = parse_design_doc(ACCEPTANCE_DESIGN_DOC, design_doc_id="doc")

    for candidate in parsed.milestone_candidates:
        excerpt = ACCEPTANCE_DESIGN_DOC[candidate.span.start : candidate.span.end]
        assert candidate.source_heading in excerpt
        assert excerpt.startswith("## ")


def test_unknown_sections_are_preserved_rather_than_dropped() -> None:
    document = ACCEPTANCE_DESIGN_DOC + "\n## Cost model for 2027\n\n- Two GPUs.\n"

    parsed = parse_design_doc(document, design_doc_id="doc")

    unknown = [item.heading for item in parsed.unknown_sections]
    assert "Cost model for 2027" in unknown
    body = next(item.body for item in parsed.sections if item.heading == "Cost model for 2027")
    assert "Two GPUs." in body
    assert any(item.code == "unknown_section_preserved" for item in parsed.diagnostics)


def test_the_gawd_narrative_sections_reach_the_collections_they_belong_to() -> None:
    """These three landed in UNKNOWN, and UNKNOWN reaches no collection at all.

    Preserved is not the same as carried: the document said them and the agent
    executing it never heard them.
    """

    document = ACCEPTANCE_DESIGN_DOC + (
        "\n## 1. Theory of the System\n\n"
        "- A planner over a fixed seven-phase lifecycle.\n"
        "\n## 2. Why This Exists\n\n"
        "- Operators re-derive plan state by hand today.\n"
        "\n## 13. If I Had 2 More Weeks\n\n"
        "- A second lifecycle profile.\n"
    )

    parsed = parse_design_doc(document, design_doc_id="doc")

    assert "A planner over a fixed seven-phase lifecycle." in parsed.assumptions
    assert "A second lifecycle profile." in parsed.non_goals
    assert parsed.motivation == ("Operators re-derive plan state by hand today.",)
    assert not any(
        item.kind is SectionKind.UNKNOWN and item.heading.startswith(("1.", "2.", "13."))
        for item in parsed.sections
    )


def test_why_this_exists_is_never_read_as_an_obligation_or_an_artifact() -> None:
    """Motivation binds nothing, and every alternative home for it binds something.

    `required_artifacts` is the one that fails loudly: each line unions into the
    plan's required final artifacts, so a milestone asked to produce a sentence
    of prose can never satisfy it and delivery fails closed.
    """

    pain = "Operators re-derive plan state by hand today."
    document = ACCEPTANCE_DESIGN_DOC + f"\n## 2. Why This Exists\n\n- {pain}\n"

    parsed = parse_design_doc(document, design_doc_id="doc")

    assert parsed.motivation == (pain,)
    assert pain not in parsed.required_artifacts
    assert pain not in parsed.requirements
    assert pain not in parsed.acceptance_criteria


def test_duplicate_milestone_identifiers_are_reported_with_both_locations() -> None:
    document = ACCEPTANCE_DESIGN_DOC + (
        "\n## Milestone B: a second milestone claiming B\n\nPhase: IMPLEMENT\n"
    )

    parsed = parse_design_doc(document, design_doc_id="doc")

    duplicates = [item for item in parsed.diagnostics if item.code == "duplicate_milestone_key"]
    assert len(duplicates) == 1
    assert duplicates[0].severity is DiagnosticSeverity.ERROR
    assert duplicates[0].span is not None
    assert [item.declared_key for item in parsed.milestone_candidates].count("b") == 1


def test_malformed_dependency_is_reported_against_its_source() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace("Depends on: B, C", "Depends on: B, Z")

    parsed = parse_design_doc(document, design_doc_id="doc")

    unresolved = [item for item in parsed.diagnostics if item.code == "unresolved_dependency"]
    assert len(unresolved) == 1
    assert "'z'" in unresolved[0].message


def test_an_unparseable_phase_is_an_error_not_a_guess() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace("Phase: VERIFY", "Phase: QA")

    parsed = parse_design_doc(document, design_doc_id="doc")

    assert any(item.code == "unknown_phase" for item in parsed.diagnostics)
    verify = next(item for item in parsed.milestone_candidates if item.declared_key == "d")
    assert verify.declared_phase is None


def test_a_document_without_milestones_has_no_executable_work() -> None:
    parsed = parse_design_doc("# Just prose\n\nNothing to run.\n", design_doc_id="doc")

    assert parsed.milestone_candidates == ()
    assert any(item.code == "no_milestones" for item in parsed.diagnostics)


def test_milestone_key_normalization_folds_the_forms_authors_write() -> None:
    assert normalize_milestone_key("A") == "a"
    assert normalize_milestone_key("Milestone A") == "a"
    assert normalize_milestone_key("milestone_step_two") == "step_two"
    # Punctuation is folded to underscores rather than removed, so `M-01` and
    # `M01` stay distinct identities. Merging them would make a dependency
    # silently resolve to a milestone the author did not name; keeping them apart
    # turns the same mistake into an unresolved-dependency diagnostic.
    assert normalize_milestone_key("M-01") == "m_01"
    assert normalize_milestone_key("M01") == "m01"


def test_milestone_sections_are_recognized_as_milestones_not_unknown() -> None:
    parsed = parse_design_doc(ACCEPTANCE_DESIGN_DOC, design_doc_id="doc")

    milestone_sections = [item for item in parsed.sections if item.kind is SectionKind.MILESTONES]
    assert len(milestone_sections) == 6


def test_inferred_phase_is_marked_inferred_and_carries_its_reasoning() -> None:
    document = """# Legacy doc

## Milestone one: do the thing

Acceptance: it is done
"""
    parsed = parse_design_doc(document, design_doc_id="legacy")
    assert parsed.milestone_candidates[0].declared_phase is None

    inferred = apply_phase_inference(
        parsed,
        [
            PhaseInference(
                milestone_key="one",
                phase=LifecyclePhase.IMPLEMENT,
                confidence=0.55,
                reasoning="the title names an action",
            )
        ],
    )

    declared = inferred.milestone_candidates[0].declared_phase
    assert declared is not None
    assert declared.inferred is True
    assert declared.confidence == 0.55
    assert declared.reasoning == "the title names an action"
    assert any(item.code == "ambiguous_inferred_phase" for item in inferred.diagnostics)


def test_a_confident_inference_does_not_raise_an_ambiguity_diagnostic() -> None:
    parsed = parse_design_doc(
        "# Legacy doc\n\n## Milestone one: do the thing\n\nAcceptance: it is done\n",
        design_doc_id="legacy",
    )

    inferred = apply_phase_inference(
        parsed,
        [
            PhaseInference(
                milestone_key="one",
                phase=LifecyclePhase.IMPLEMENT,
                confidence=1.0,
                reasoning="an operator confirmed this classification",
            )
        ],
    )

    assert not any(item.code == "ambiguous_inferred_phase" for item in inferred.diagnostics)


def test_a_display_banner_never_outranks_an_explicit_target_project() -> None:
    """`Project:` is a loose alias and the GAWD banner matches it.

    The banner line reads `**Project:** X | **Version:** Y | ...`, so the alias
    captures the whole pipe-separated remainder as a project id. Which one won
    used to be decided by whichever appeared first in the file, which made the
    hand-authored docs work by luck and every generated one compile blocked.
    """

    banner_first = parse_design_doc(
        "# Doc\n\n**Project:** shiny | **Version:** v4 | **Status:** DRAFT\n\n"
        "Target project: local-first-agent-os\n\n## 1. Theory\n\nprose\n",
        design_doc_id="banner_first",
    )
    assert banner_first.declared_target_project_id == "local-first-agent-os"

    # The alias still works when it is the only thing the document says.
    alias_only = parse_design_doc(
        "# Doc\n\nProject: local_first_agent_os\n\n## 1. Theory\n\nprose\n",
        design_doc_id="alias_only",
    )
    assert alias_only.declared_target_project_id == "local_first_agent_os"


def test_the_banner_alone_names_the_project_it_meant() -> None:
    """The template's own header must not be a trap for documents it generates.

    `**Project:** X | **Version:** Y | **Status:** Z` is what this repository's
    GAWD template writes. Read literally the alias handed back everything after
    `Project:`, so a document whose only declaration was its banner compiled
    blocked with `target project '** X | **Version:** ...' is not registered` -
    a message that names the symptom and leaves the cause to be guessed.

    Requiring a second `Target project:` line instead would be a rule that every
    document the system writes for itself breaks.
    """

    banner_only = parse_design_doc(
        "# Doc\n\n**Project:** local_first_agent_os | **Version:** v1 | "
        "**Status:** DRAFT (operator review required) | **Date:** 2026-08-11\n\n"
        "## 1. Theory\n\nprose\n",
        design_doc_id="banner_only",
    )

    assert banner_only.declared_target_project_id == "local_first_agent_os"


# --------------------------------------------------------------------------- #
# Fenced text is quotation
# --------------------------------------------------------------------------- #


def _parse(text: str):
    return parse_design_doc(text, design_doc_id="doc", source_path=None)


def test_a_fenced_milestone_is_an_example_not_a_declaration() -> None:
    """The sparse template's own trap, caught where the template promised.

    The template fences a `### Milestone 0:` example and its comment says the
    fence keeps it an example. Until fences became quotation that was false:
    every draft compiled with a fake PLAN milestone named "Write the title as
    an outcome, not a task" that nobody wrote.
    """

    document = (
        ACCEPTANCE_DESIGN_DOC
        + """

## Authoring Notes

Copy the shape below, once per milestone.

```markdown
### Milestone 0: Write the title as an outcome, not a task

Phase: PLAN
Acceptance: one checkable sentence per line
Artifacts: implementation_plan
```
"""
    )

    parsed = _parse(document)

    keys = [candidate.declared_key for candidate in parsed.milestone_candidates]
    assert "0" not in keys
    assert keys == ["a", "b", "c", "d", "e", "f"]


def test_a_fenced_section_heading_is_not_a_section_boundary() -> None:
    """A quoted `## Permission Envelope` must not duplicate the real one."""

    document = (
        ACCEPTANCE_DESIGN_DOC
        + """

## Permission Envelope

Autonomous permissions:
- read_repo_context

## Review Transcript

The senior's reply, quoted verbatim:

```markdown
I restated the contract.

## Permission Envelope

Autonomous permissions:
- deploy
```
"""
    )

    parsed = _parse(document)

    envelope_sections = [
        section for section in parsed.sections if section.kind is SectionKind.PERMISSION_ENVELOPE
    ]
    assert len(envelope_sections) == 1
    assert not any(item.code == "duplicate_permission_envelope" for item in parsed.diagnostics)
    assert parsed.permission_envelope is not None
    assert all(action.value != "deploy" for action in parsed.permission_envelope.autonomous)


def test_a_fenced_field_inside_a_milestone_body_stays_an_example() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace(
        "Phase: PLAN\n",
        """Phase: PLAN
Description: the field syntax is, for example:

```text
Executor: review.operator
Approval: required
```

""",
        1,
    )

    parsed = _parse(document)

    milestone = next(c for c in parsed.milestone_candidates if c.declared_key == "a")
    assert milestone.executor_kind is None
    assert milestone.requires_operator_approval is False


def test_an_unterminated_fence_quotes_to_the_end_of_the_document() -> None:
    """What the fence means in rendered Markdown, applied rather than repaired."""

    document = (
        ACCEPTANCE_DESIGN_DOC
        + """

## Trailing Example

```markdown
### Milestone 99: never real

Phase: IMPLEMENT
Acceptance: never
"""
    )

    parsed = _parse(document)

    assert "99" not in [candidate.declared_key for candidate in parsed.milestone_candidates]


def test_fence_masking_does_not_shift_source_spans() -> None:
    """Diagnostics must keep pointing at real characters of the original text."""

    document = (
        ACCEPTANCE_DESIGN_DOC
        + """

```text
a fenced digression long enough to shift offsets if masking ever changed length
```

### Milestone Z: declared after the fence

Phase: IMPLEMENT
Depends on: nothing_declares_this
Acceptance: compiles
"""
    )

    parsed = _parse(document)

    diagnostic = next(item for item in parsed.diagnostics if item.code == "unresolved_dependency")
    assert diagnostic.span is not None
    assert "Milestone Z" in document[diagnostic.span.start : diagnostic.span.end]


# --------------------------------------------------------------------------- #
# Headings match exactly, or not at all
# --------------------------------------------------------------------------- #


def test_a_heading_containing_a_known_phrase_is_not_that_section() -> None:
    """The substring traps, closed.

    Each of these classified as a real section because it contains a known
    phrase: a proposed envelope became the envelope, an exclusion list became
    requirements, a class name became rollout policy.
    """

    document = (
        ACCEPTANCE_DESIGN_DOC
        + """

## Permission Envelope (Proposed)

Autonomous permissions:
- deploy

## Not in scope

- Everything here is excluded.

## RollbackAdapter

A class, not a rollout plan.
"""
    )

    parsed = _parse(document)

    assert parsed.permission_envelope is None
    assert "Everything here is excluded." not in parsed.requirements
    assert parsed.rollout == ()
    unknown = {section.heading for section in parsed.unknown_sections}
    assert {"Permission Envelope (Proposed)", "Not in scope", "RollbackAdapter"} <= unknown


def test_an_alias_heading_classifies_and_names_its_canonical_form() -> None:
    document = (
        ACCEPTANCE_DESIGN_DOC
        + """

## Open Questions

- BLOCKING: which registry does this bind to?
"""
    )

    parsed = _parse(document)

    assert any("which registry" in item for item in parsed.unresolved_questions)
    alias = next(item for item in parsed.diagnostics if item.code == "alias_heading")
    assert alias.severity is DiagnosticSeverity.INFO
    assert "unresolved questions" in alias.message


def test_a_canonical_heading_emits_no_alias_diagnostic() -> None:
    parsed = _parse(ACCEPTANCE_DESIGN_DOC)

    assert not any(item.code == "alias_heading" for item in parsed.diagnostics)


def test_a_dated_audit_heading_is_unknown_by_design() -> None:
    """The date belongs in the body; an exact table does not enumerate days."""

    document = (
        ACCEPTANCE_DESIGN_DOC
        + """

## Current State Verification - 2026-08-17

- The runtime is stopped.
"""
    )

    parsed = _parse(document)

    assert "The runtime is stopped." not in parsed.assumptions
    assert any(
        section.heading == "Current State Verification - 2026-08-17"
        for section in parsed.unknown_sections
    )
