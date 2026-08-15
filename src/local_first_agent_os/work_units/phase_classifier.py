# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Propose a lifecycle phase for a milestone that declared none.

A milestone written as prose says what to do and not when in the lifecycle it
happens. Everything else about such a milestone is recoverable deterministically:
the text is its own acceptance criterion, and the executor registry declares the
evidence. The phase is the one field that needs judgment, which is why it is the
only thing here that asks a model.

The safety comes from `apply_phase_inference`, not from this module. Whatever is
returned arrives as a proposal carrying confidence and reasoning, is stamped
`inferred=True` so it can never be mistaken for a declaration, and is turned into
an execution blocker below the confirmation threshold. So the worst a bad
classification can do is block a compile, never start work under a wrong phase.

`LifecyclePhase` has seven members and the whole task is choosing one of them,
which is why the model is asked for a single token rather than for prose to
parse. An answer that is not a phase name is discarded rather than repaired: a
missing inference blocks compilation with `missing_phase`, which is the honest
outcome, and guessing on the model's behalf would defeat the point of the
confidence gate.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .design_doc import MilestoneCandidate, ParsedDesignDoc, PhaseInference
from .lifecycle import ORDERED_PHASES, LifecyclePhase

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemma4"
DEFAULT_BASE_URL = "http://127.0.0.1:8080"

# Below the threshold `apply_phase_inference` confirms at, so a classifier that
# declines to commit produces a blocker rather than a silent decision.
UNCERTAIN_CONFIDENCE = 0.5

# Headroom, not a working budget. With thinking disabled a classification costs
# about 46 completion tokens; this is sized for a router that ignores
# `chat_template_kwargs` and reasons anyway, where the answer arrives only after
# ~940 tokens of deliberation. Undersized, that case returns an empty string and
# `finish_reason: "length"` rather than an error, so the headroom is what keeps a
# router without thinking control working instead of silently classifying nothing.
DEFAULT_MAX_TOKENS = 2500


class PhaseClassifier(Protocol):
    """Propose phases for the candidates that declared none."""

    def classify(self, candidates: Sequence[MilestoneCandidate]) -> tuple[PhaseInference, ...]: ...


def unphased_candidates(parsed: ParsedDesignDoc) -> tuple[MilestoneCandidate, ...]:
    """The candidates a classifier should be asked about.

    A declared phase is never reconsidered. `apply_phase_inference` would ignore
    the proposal anyway, and asking would spend a model call to be overruled.
    """

    return tuple(item for item in parsed.milestone_candidates if item.declared_phase is None)


def phase_prompt(candidate: MilestoneCandidate) -> str:
    """One milestone, one question, one word back.

    The phase list is rendered from `ORDERED_PHASES` rather than written out, so
    a phase added to the lifecycle cannot go missing from the prompt while the
    enum accepts it.
    """

    names = ", ".join(phase.value for phase in ORDERED_PHASES)
    return (
        "Classify this engineering milestone into exactly one lifecycle phase.\n\n"
        f"Phases, in order: {names}\n\n"
        f"Milestone: {candidate.title}\n"
        f"Detail: {candidate.description}\n\n"
        "Answer with a JSON object and nothing else, in this exact shape:\n"
        '{"phase": "<one phase name>", "confidence": <0.0 to 1.0>, '
        '"reasoning": "<one sentence>"}'
    )


def parse_phase_answer(text: str) -> tuple[LifecyclePhase, float, str] | None:
    """Read a classification, or decide the answer was not one.

    Returns None rather than raising, and rather than defaulting to a phase. A
    model that answers off-contract has failed to classify, and the honest
    consequence is the milestone staying unphased so the compiler blocks on it.
    """

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        phase = LifecyclePhase(str(payload.get("phase", "")).strip().upper())
    except ValueError:
        return None
    try:
        confidence = float(payload.get("confidence", UNCERTAIN_CONFIDENCE))
    except (TypeError, ValueError):
        confidence = UNCERTAIN_CONFIDENCE
    reasoning = str(payload.get("reasoning", "")).strip() or "no reasoning given"
    return phase, max(0.0, min(1.0, confidence)), reasoning


@dataclass
class LocalModelPhaseClassifier:
    """Classify with the local router, one milestone per call.

    One call per milestone rather than one call for the document: a single
    response covering six milestones has to be parsed positionally, and a model
    that drops one shifts every phase after it onto the wrong milestone. Separate
    calls make a failure local to the milestone that caused it.

    The model is local by default, which is what makes this affordable to run on
    every compile and keeps the document on this machine.
    """

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 300.0
    max_tokens: int = DEFAULT_MAX_TOKENS
    client_factory: Callable[..., Any] = httpx.Client

    def classify(self, candidates: Sequence[MilestoneCandidate]) -> tuple[PhaseInference, ...]:
        if not candidates:
            return ()
        inferences: list[PhaseInference] = []
        with self.client_factory(base_url=self.base_url, timeout=self.timeout_seconds) as client:
            for candidate in candidates:
                answer = self._ask(client, candidate)
                if answer is None:
                    continue
                phase, confidence, reasoning = answer
                inferences.append(
                    PhaseInference(
                        milestone_key=candidate.declared_key,
                        phase=phase,
                        confidence=confidence,
                        reasoning=reasoning,
                    )
                )
        return tuple(inferences)

    def _ask(
        self,
        client: Any,
        candidate: MilestoneCandidate,
    ) -> tuple[LifecyclePhase, float, str] | None:
        """One classification, or None if the model did not produce one.

        A transport failure is not distinguished from an off-contract answer on
        purpose: both mean this milestone has no proposed phase, and both lead to
        the same `missing_phase` blocker with the milestone named.
        """

        try:
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": phase_prompt(candidate)}],
                    "temperature": 0,
                    "max_tokens": self.max_tokens,
                    # Choosing one of seven enum values is not a task that repays
                    # deliberation, and gemma4 spent 938 completion tokens on it
                    # against 46 with thinking off: 87.6s versus 4.9s for the same
                    # answer. A model that ignores this key is unaffected, so the
                    # request stays valid across routers that do not support it.
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            )
            response.raise_for_status()
            choice = response.json()["choices"][0]
            content = str(choice["message"].get("content") or "")
            if not content and choice.get("finish_reason") == "length":
                # A reasoning model that ran out of budget mid-thought answers
                # HTTP 200 with an empty string, which is indistinguishable from
                # a refusal unless the finish reason is read. Say so, because the
                # fix is a bigger budget and nothing about the milestone.
                logger.warning(
                    "phase classification for %s exhausted %d tokens before answering",
                    candidate.declared_key,
                    self.max_tokens,
                )
                return None
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            return None
        return parse_phase_answer(content)


def classify_missing_phases(
    parsed: ParsedDesignDoc,
    classifier: PhaseClassifier | None = None,
) -> tuple[PhaseInference, ...]:
    """Propose a phase for every candidate that declared none."""

    candidates = unphased_candidates(parsed)
    if not candidates:
        return ()
    return (classifier or LocalModelPhaseClassifier()).classify(candidates)


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "UNCERTAIN_CONFIDENCE",
    "LocalModelPhaseClassifier",
    "PhaseClassifier",
    "classify_missing_phases",
    "parse_phase_answer",
    "phase_prompt",
    "unphased_candidates",
]
