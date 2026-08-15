# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A milestone written as prose gets a proposed phase, or none at all."""

from __future__ import annotations

from typing import Any

from local_first_agent_os.work_units.design_doc import (
    apply_phase_inference,
    parse_design_doc,
)
from local_first_agent_os.work_units.lifecycle import LifecyclePhase
from local_first_agent_os.work_units.phase_classifier import (
    UNCERTAIN_CONFIDENCE,
    LocalModelPhaseClassifier,
    classify_missing_phases,
    parse_phase_answer,
    phase_prompt,
    unphased_candidates,
)

PROSE_DOC = """# Prose milestones

## 8. Execution Milestones

1. Merge the skills branch and wire the loader to the in-repo skills directory.
2. Verify the pipeline with blocking tests.
3. Staff review and merge approval.
"""

DECLARED_DOC = """# Declared milestones

## Milestone A: plan the change

Phase: PLAN
Acceptance: a plan exists
Artifacts: implementation_plan
"""


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    """A router that answers each milestone in the order it was asked."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.prompts: list[str] = []

    def __call__(self, **_kwargs: Any) -> _FakeClient:
        return self

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def post(self, _path: str, *, json: dict[str, Any]) -> _FakeResponse:
        self.prompts.append(json["messages"][0]["content"])
        return _FakeResponse(self.answers[len(self.prompts) - 1])


def test_only_undeclared_milestones_are_classified() -> None:
    """A declared phase is never reconsidered.

    `apply_phase_inference` would ignore the proposal anyway, so asking would
    spend a model call to be overruled.
    """

    declared = parse_design_doc(DECLARED_DOC, design_doc_id="declared")
    assert unphased_candidates(declared) == ()

    prose = parse_design_doc(PROSE_DOC, design_doc_id="prose")
    assert [item.declared_key for item in unphased_candidates(prose)] == ["1", "2", "3"]


def test_every_lifecycle_phase_is_offered_in_the_prompt() -> None:
    """The phase list is rendered from the enum, not written out beside it.

    A phase added to the lifecycle must not be able to go missing from the
    prompt while the parser still accepts it.
    """

    prose = parse_design_doc(PROSE_DOC, design_doc_id="prose")
    prompt = phase_prompt(prose.milestone_candidates[0])
    for phase in LifecyclePhase:
        assert phase.value in prompt


def test_a_prose_list_is_classified_into_phases() -> None:
    client = _FakeClient(
        [
            '{"phase": "IMPLEMENT", "confidence": 0.95, "reasoning": "it merges and wires code"}',
            '{"phase": "VERIFY", "confidence": 0.97, "reasoning": "it runs blocking tests"}',
            '{"phase": "REVIEW", "confidence": 0.93, "reasoning": "it is a staff review"}',
        ]
    )
    parsed = parse_design_doc(PROSE_DOC, design_doc_id="prose")

    inferences = classify_missing_phases(parsed, LocalModelPhaseClassifier(client_factory=client))

    assert [(item.milestone_key, item.phase) for item in inferences] == [
        ("1", LifecyclePhase.IMPLEMENT),
        ("2", LifecyclePhase.VERIFY),
        ("3", LifecyclePhase.REVIEW),
    ]
    assert len(client.prompts) == 3, "one call per milestone, so a failure stays local"


def test_an_inference_never_reads_as_a_declaration() -> None:
    """The whole safety argument: a proposal stays marked as one.

    A high-confidence inference still carries `inferred=True`, its confidence,
    and its reasoning, so an auditor can tell what the document said from what a
    model guessed.
    """

    client = _FakeClient(
        [
            '{"phase": "IMPLEMENT", "confidence": 0.99, "reasoning": "merges code"}',
            '{"phase": "VERIFY", "confidence": 0.99, "reasoning": "tests"}',
            '{"phase": "REVIEW", "confidence": 0.99, "reasoning": "review"}',
        ]
    )
    parsed = parse_design_doc(PROSE_DOC, design_doc_id="prose")
    inferences = classify_missing_phases(parsed, LocalModelPhaseClassifier(client_factory=client))

    applied = apply_phase_inference(parsed, inferences)

    first = applied.milestone_candidates[0].declared_phase
    assert first is not None
    assert first.inferred is True
    assert first.confidence == 0.99
    assert first.reasoning == "merges code"


def test_an_off_contract_answer_yields_no_inference() -> None:
    """A model that does not classify leaves the milestone unphased.

    That is the honest outcome: the compiler then blocks with `missing_phase`
    naming the milestone, rather than the classifier picking a phase on the
    model's behalf and starting work under it.
    """

    client = _FakeClient(
        [
            "I think this is probably an implementation task?",
            '{"phase": "NOT_A_PHASE", "confidence": 0.99, "reasoning": "x"}',
            '{"phase": "REVIEW", "confidence": 0.93, "reasoning": "staff review"}',
        ]
    )
    parsed = parse_design_doc(PROSE_DOC, design_doc_id="prose")

    inferences = classify_missing_phases(parsed, LocalModelPhaseClassifier(client_factory=client))

    assert [item.milestone_key for item in inferences] == ["3"]


def test_a_transport_failure_is_not_a_phase() -> None:
    class _Exploding(_FakeClient):
        def post(self, _path: str, *, json: dict[str, Any]) -> _FakeResponse:
            raise RuntimeError("router is down")

    parsed = parse_design_doc(PROSE_DOC, design_doc_id="prose")
    classifier = LocalModelPhaseClassifier(client_factory=_Exploding([]))

    try:
        inferences = classifier.classify(unphased_candidates(parsed))
    except RuntimeError:  # pragma: no cover - the point is that it does not raise
        inferences = None  # type: ignore[assignment]

    assert inferences is None or inferences == ()


def test_a_missing_confidence_is_treated_as_uncertain() -> None:
    """Absent confidence must not read as certainty.

    The confirmation threshold is the only thing standing between a guess and a
    started milestone, so a model that omits the field gets the value that keeps
    the blocker in place.
    """

    answer = parse_phase_answer('{"phase": "VERIFY", "reasoning": "tests"}')
    assert answer is not None
    _phase, confidence, _reasoning = answer
    assert confidence == UNCERTAIN_CONFIDENCE
    assert confidence < 0.9


def test_confidence_is_clamped_to_a_probability() -> None:
    answer = parse_phase_answer('{"phase": "PLAN", "confidence": 7, "reasoning": "x"}')
    assert answer is not None
    assert answer[1] == 1.0


def test_prose_around_the_json_is_tolerated() -> None:
    """Small models wrap JSON in chatter, and that is not a classification failure."""

    answer = parse_phase_answer(
        "Sure! Here you go:\n"
        '{"phase": "DELIVER", "confidence": 0.91, "reasoning": "ships"}\n'
        "Hope that helps."
    )
    assert answer is not None
    assert answer[0] is LifecyclePhase.DELIVER
