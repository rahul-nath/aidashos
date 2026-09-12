# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import pytest

from local_first_agent_os.coordination.failures import (
    FailureClassificationSource,
    classify_failure_with_source,
)
from local_first_agent_os.coordination.outcomes import TerminalOutcome, failure_category
from local_first_agent_os.pow_wow.failure_classifier import (
    NONE_OF_THESE,
    JuniorFailureClassifier,
    failure_classification_outcomes,
    failure_classification_prompt,
    parse_failure_classification_answer,
)
from local_first_agent_os.staffing import DEFAULT_BENCH


def test_prompt_derives_the_complete_failure_vocabulary_and_none_of_these() -> None:
    prompt = failure_classification_prompt("opaque provider text")
    expected = {
        outcome
        for outcome in TerminalOutcome
        if outcome is not TerminalOutcome.UNKNOWN_FAILURE and failure_category(outcome) is not None
    }

    assert set(failure_classification_outcomes()) == expected
    assert all(outcome.value in prompt for outcome in expected)
    assert NONE_OF_THESE in prompt
    assert TerminalOutcome.UNKNOWN_FAILURE.value not in prompt
    assert "Do not guess a nearest match" in prompt


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ('{"outcome":"POLICY_DENIED"}', TerminalOutcome.POLICY_DENIED),
        (f'{{"outcome":"{NONE_OF_THESE}"}}', TerminalOutcome.UNKNOWN_FAILURE),
        ('{"outcome":"AUTOMATED_COMPLETION"}', None),
        ('{"outcome":"MADE_UP_FAILURE"}', None),
        ('{"wrong_key":"POLICY_DENIED"}', None),
        ("not json", None),
    ],
)
def test_parser_accepts_only_failure_outcomes_or_none_of_these(
    answer: str,
    expected: TerminalOutcome | None,
) -> None:
    assert parse_failure_classification_answer(answer) is expected


def test_junior_classifier_records_a_bounded_scoped_delegate_call() -> None:
    calls: list[dict[str, object]] = []

    def delegate(
        *, prompt, task_name="", model=None, model_params=None, timeout_seconds=None, pow_wow_id=""
    ) -> dict[str, object]:
        calls.append(
            dict(
                prompt=prompt,
                task_name=task_name,
                model=model,
                model_params=model_params,
                timeout_seconds=timeout_seconds,
                pow_wow_id=pow_wow_id,
            )
        )
        return {"ok": True, "output": '{"outcome":"POLICY_DENIED"}'}

    classifier = JuniorFailureClassifier(
        delegate_fn=delegate,
        bench=DEFAULT_BENCH,
        pow_wow_id="pow-1",
        source_task_name="staff_review",
    )

    assert classifier("ruleset ZEBRA-17 denied the request") is TerminalOutcome.POLICY_DENIED
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == 30.0
    assert calls[0]["pow_wow_id"] == "pow-1"
    assert calls[0]["task_name"] == "failure_classifier_staff_review"


@pytest.mark.parametrize(
    "delegate",
    [
        lambda **_kwargs: {"ok": False, "error": "model unavailable"},
        lambda **_kwargs: {"ok": True, "output": "unparseable"},
    ],
)
def test_unavailable_or_unparseable_junior_answer_returns_no_classification(delegate) -> None:
    classifier = JuniorFailureClassifier(
        delegate_fn=delegate,
        bench=DEFAULT_BENCH,
        pow_wow_id="pow-1",
        source_task_name="staff_review",
    )

    evidence = "opaque provider text"

    classification = classify_failure_with_source(
        evidence,
        operation="run_provider",
        unknown_classifier=classifier,
    )
    assert classification.failure.error_code == TerminalOutcome.UNKNOWN_FAILURE
    assert classification.failure.message == evidence
    assert classification.source is FailureClassificationSource.UNCLASSIFIED
