# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The registry's reasoning contract, and how it reaches a model.

`reasoning` lived only in `configs/model_registry.toml` until 2026-08-14, read
straight off the raw TOML by the preset generator. `ModelSpec` never declared it,
so pydantic dropped it silently and `DEFAULT_MODELS` could not express it, which
meant the Python fallback registry would have brought every model up thinking.
These tests hold the field in the typed contract and pin its translation.
"""

from __future__ import annotations

import pytest

from local_first_agent_os.contracts import (
    ModelCallRequest,
    ModelRole,
    ModelSpec,
    ReasoningPolicy,
)
from local_first_agent_os.model_manager import ModelManager
from local_first_agent_os.model_registry import DEFAULT_MODELS


def _glimmer(reasoning: str) -> ModelSpec:
    return ModelSpec(
        alias="glimmer_deliberator",
        role=ModelRole.DELIBERATOR,
        model_id="muse-glimmer-30b-kquant-dynamic",
        server_model_name="glimmer",
        reasoning=ReasoningPolicy.model_validate(reasoning),
        reasoning_dialect="reasoning_strength",
    )


@pytest.mark.parametrize(
    ("shorthand", "mode", "budget"),
    [("off", "off", None), ("full", "full", None), ("bounded(256)", "bounded", 256)],
)
def test_config_shorthand_parses_into_the_contract(
    shorthand: str, mode: str, budget: int | None
) -> None:
    policy = ReasoningPolicy.model_validate(shorthand)
    assert (policy.mode, policy.budget_tokens) == (mode, budget)


@pytest.mark.parametrize("bad", ["bounded()", "bounded(-1)", "sometimes", "on", ""])
def test_an_unreadable_reasoning_value_is_refused_rather_than_ignored(bad: str) -> None:
    """Crash on an illegal state instead of coercing it.

    A typo here would otherwise mean the model quietly keeps the template
    default, which for Muse-Glimmer is `high` - the most expensive setting, and
    the opposite of what an operator writing this field is reaching for.
    """

    with pytest.raises(ValueError):
        ReasoningPolicy.model_validate(bad)


def test_a_budget_only_means_something_on_bounded() -> None:
    with pytest.raises(ValueError):
        ReasoningPolicy(mode="off", budget_tokens=256)
    with pytest.raises(ValueError):
        ReasoningPolicy(mode="bounded")


@pytest.mark.parametrize(
    ("shorthand", "strength"),
    [
        ("off", "none"),
        ("bounded(256)", "low"),
        ("bounded(1024)", "medium"),
        ("full", "high"),
    ],
)
def test_glimmer_reasoning_translates_to_its_own_template_variable(
    shorthand: str, strength: str
) -> None:
    """Muse-Glimmer's template contains no `enable_thinking`, only
    `reasoning_strength`, so the obvious Qwen-shaped key would be accepted by the
    server and ignored by the template while the model thought anyway."""

    overrides = _glimmer(shorthand).reasoning_request_overrides()
    assert overrides == {"chat_template_kwargs": {"reasoning_strength": strength}}


def test_a_model_with_no_dialect_sends_no_per_request_reasoning() -> None:
    """Silence, not a guess. gemma4 and qwen3.8 carry `reasoning = off` for the
    server preset and expose no verified per-request lever, so inventing one
    would put an unread key in every request they receive."""

    spec = ModelSpec(
        alias="general_gemma4",
        role=ModelRole.GENERAL,
        model_id="gemma-4-e4b-q4-k-m",
        server_model_name="gemma4",
        reasoning=ReasoningPolicy(mode="off"),
    )
    assert spec.reasoning_request_overrides() == {}


def test_an_explicit_call_outranks_the_registry_default() -> None:
    """The policy is a default for callers that do not care. A task that decided
    it deserves deliberation says so per call, and must not have that decision
    overwritten by the registry it is deliberately departing from."""

    req = ModelCallRequest(
        workflow_id="wf",
        model_role=ModelRole.DELIBERATOR,
        input_artifact_id="artifact",
        payload={},
        params={"chat_template_kwargs": {"reasoning_strength": "high"}},
    )
    body = ModelManager._body_for_request(req, _glimmer("off"), [])
    assert body["chat_template_kwargs"] == {"reasoning_strength": "high"}


def test_overriding_the_band_keeps_unrelated_template_arguments() -> None:
    """`chat_template_kwargs` merges key-by-key. Replacing the dict wholesale
    would make setting the reasoning band silently drop anything else the caller
    put in it."""

    req = ModelCallRequest(
        workflow_id="wf",
        model_role=ModelRole.DELIBERATOR,
        input_artifact_id="artifact",
        payload={},
        params={"chat_template_kwargs": {"custom_flag": True}},
    )
    body = ModelManager._body_for_request(req, _glimmer("off"), [])
    assert body["chat_template_kwargs"] == {
        "reasoning_strength": "none",
        "custom_flag": True,
    }


def test_the_default_registry_can_express_reasoning() -> None:
    """The regression this module exists for: `DEFAULT_MODELS` is what the
    registry falls back to when the TOML declares no `[models]`, so a reasoning
    setting it cannot hold is a setting that vanishes exactly when the config
    file does."""

    by_alias = {spec.alias: spec for spec in DEFAULT_MODELS}
    # Keyed by alias, not by served name: `context_compactor` serves the same
    # `gemma4` GGUF under the same server_model_name, so a name-keyed lookup
    # silently returns whichever entry is declared last.
    general = by_alias["general_gemma4"]
    assert general.reasoning is not None
    assert general.reasoning.mode == "off"
    glimmer = by_alias["glimmer_deliberator"]
    assert glimmer.reasoning is not None
    assert glimmer.reasoning.mode == "off"
    assert glimmer.reasoning_dialect == "reasoning_strength"
