# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Catalog validation: every invalid rule file must fail at load, not at 3am.

The design's rule is that an invalid or ambiguous catalog is a crash rather
than a severe-log, because a feedback loop that is quietly off is
indistinguishable from one with nothing to report.  Each test here names one
way an operator can get the file wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_first_agent_os.coordination.outcomes import FailureCategory
from local_first_agent_os.monitor_feedback.rules import (
    FeedbackResponse,
    FeedbackRule,
    FeedbackRuleCatalog,
    FeedbackRuleError,
    load_feedback_rules,
)
from local_first_agent_os.monitor_feedback.signals import (
    EvidenceRef,
    LedgerFactKind,
    LedgerFactSignal,
    Severity,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

_VALID = """
schema_version = "feedback_rules.v1"
global_open_intent_cap = 5

[[rule]]
rule_id = "base"
response = "advisory"
tier = "junior"
cooldown_seconds = 60
daily_cap = 2
prompt_template = "diagnose {error_code}"

  [rule.selector]
  signal_kind = "MILESTONE_FAILED"
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "feedback_rules.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _signal(
    kind: LedgerFactKind = LedgerFactKind.MILESTONE_FAILED,
    *,
    project: str | None = "pest_site_factory",
    severity: Severity = Severity.WARNING,
    category: FailureCategory | None = FailureCategory.INFRASTRUCTURE,
) -> LedgerFactSignal:
    return LedgerFactSignal(
        kind=kind,
        severity=severity,
        identity=("saga-1", "milestone-1", "PROCESS_FAILED"),
        observed_at=1000.0,
        evidence=EvidenceRef(table="saga_milestones", row_id="milestone-1"),
        target_project_id=project,
        failure_category=category,
        error_code="PROCESS_FAILED",
    )


def _matched(catalog: FeedbackRuleCatalog, signal: LedgerFactSignal) -> FeedbackRule:
    """match() returns None when no rule applies; these cases all expect a hit.

    Asserting it here keeps the expectation in one place and lets the call sites
    stay one line each.
    """

    rule = catalog.match(signal)
    assert rule is not None
    return rule


def test_the_shipped_catalog_is_valid() -> None:
    """The starter catalog is part of the product, so it is part of the suite."""

    catalog = load_feedback_rules(REPO_ROOT / "configs" / "feedback_rules.toml")
    assert catalog.rules
    assert all(rule.tier.value for rule in catalog.rules)
    assert catalog.global_open_intent_cap >= 1


def test_a_valid_catalog_loads(tmp_path: Path) -> None:
    catalog = load_feedback_rules(_write(tmp_path, _VALID))
    assert [rule.rule_id for rule in catalog.rules] == ["base"]
    assert catalog.rules[0].response is FeedbackResponse.ADVISORY


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        pytest.param(
            _VALID.replace('schema_version = "feedback_rules.v1"', 'schema_version = "v0"'),
            "declares schema_version",
            id="wrong_schema_version",
        ),
        pytest.param(
            _VALID.replace("global_open_intent_cap = 5", "global_open_intent_cap = 0"),
            "global_open_intent_cap",
            id="zero_global_cap",
        ),
        pytest.param(
            _VALID.replace('response = "advisory"', 'response = "auto_merge"'),
            "invalid response",
            id="unknown_response",
        ),
        pytest.param(
            _VALID.replace('tier = "junior"', 'tier = "principal"'),
            "invalid tier",
            id="unknown_tier",
        ),
        pytest.param(
            _VALID.replace("daily_cap = 2", "daily_cap = 0"),
            "daily_cap must be an integer >= 1",
            id="zero_daily_cap",
        ),
        pytest.param(
            _VALID.replace("cooldown_seconds = 60", "cooldown_seconds = -1"),
            "cooldown_seconds must be a number >= 0",
            id="negative_cooldown",
        ),
        pytest.param(
            _VALID.replace('prompt_template = "diagnose {error_code}"', ""),
            "no prompt_template",
            id="advisory_without_prompt",
        ),
        pytest.param(
            _VALID.replace("{error_code}", "{not_a_field}"),
            "not renderable",
            id="unrenderable_prompt",
        ),
        pytest.param(
            _VALID.replace('signal_kind = "MILESTONE_FAILED"', 'signal_kind = "COSMIC_RAY"'),
            "invalid selector.signal_kind",
            id="unknown_signal_kind",
        ),
        pytest.param(
            _VALID.replace(
                'signal_kind = "MILESTONE_FAILED"',
                'signal_kind = "MILESTONE_FAILED"\n  colour = "blue"',
            ),
            "selector has unknown keys",
            id="unknown_selector_key",
        ),
        pytest.param(
            _VALID.replace('rule_id = "base"', 'rule_id = "base"\nretries = 3'),
            "has unknown keys",
            id="unknown_rule_key",
        ),
        pytest.param(
            _VALID.replace('rule_id = "base"', 'rule_id = ""'),
            "non-empty rule_id",
            id="empty_rule_id",
        ),
        pytest.param(
            _VALID + _VALID.split("global_open_intent_cap = 5")[1].replace('"base"', '"twin"'),
            "identical selectors",
            id="identical_selectors",
        ),
        pytest.param(
            'schema_version = "feedback_rules.v1"\nglobal_open_intent_cap = 5\n',
            "defines no rules",
            id="no_rules",
        ),
        pytest.param(
            "this is not toml {{{",
            "not valid TOML",
            id="malformed_toml",
        ),
    ],
)
def test_invalid_catalogs_fail_at_load(tmp_path: Path, mutation: str, expected: str) -> None:
    with pytest.raises(FeedbackRuleError, match=expected):
        load_feedback_rules(_write(tmp_path, mutation))


def test_a_missing_catalog_is_an_error_not_an_empty_catalog(tmp_path: Path) -> None:
    """An empty catalog would silently disable the loop; a missing file says so."""

    with pytest.raises(FeedbackRuleError, match="not found"):
        load_feedback_rules(tmp_path / "absent.toml")


def test_duplicate_rule_ids_are_rejected(tmp_path: Path) -> None:
    body = (
        _VALID
        + """
[[rule]]
rule_id = "base"
response = "digest_only"
tier = "junior"
daily_cap = 1

  [rule.selector]
  signal_kind = "DISPATCH_INTENT_FAILED"
"""
    )
    with pytest.raises(FeedbackRuleError, match="duplicate rule_id"):
        load_feedback_rules(_write(tmp_path, body))


def test_a_disabled_rule_may_duplicate_an_enabled_selector(tmp_path: Path) -> None:
    """Disabling is how an operator parks a rule, so it must not be a crash."""

    body = (
        _VALID
        + """
[[rule]]
rule_id = "parked"
enabled = false
response = "advisory"
tier = "junior"
daily_cap = 1
prompt_template = "diagnose {error_code}"

  [rule.selector]
  signal_kind = "MILESTONE_FAILED"
"""
    )
    catalog = load_feedback_rules(_write(tmp_path, body))
    assert _matched(catalog, _signal()).rule_id == "base"


def test_the_most_specific_rule_takes_the_signal(tmp_path: Path) -> None:
    body = (
        _VALID
        + """
[[rule]]
rule_id = "narrow"
response = "advisory"
tier = "junior"
daily_cap = 1
prompt_template = "diagnose {error_code}"

  [rule.selector]
  signal_kind = "MILESTONE_FAILED"
  target_project_id = "pest_site_factory"
"""
    )
    catalog = load_feedback_rules(_write(tmp_path, body))
    assert _matched(catalog, _signal(project="pest_site_factory")).rule_id == "narrow"
    assert _matched(catalog, _signal(project="other")).rule_id == "base"


def test_an_ambiguous_match_crashes_rather_than_picking_by_file_order(tmp_path: Path) -> None:
    """Two different selectors of equal specificity can both match one signal.

    Load-time validation cannot see this because it depends on the signal, so
    the catalog's ``match`` is the second half of the same contract.
    """

    body = """
schema_version = "feedback_rules.v1"
global_open_intent_cap = 5

[[rule]]
rule_id = "by_project"
response = "advisory"
tier = "junior"
daily_cap = 1
prompt_template = "p"

  [rule.selector]
  target_project_id = "pest_site_factory"

[[rule]]
rule_id = "by_severity"
response = "advisory"
tier = "junior"
daily_cap = 1
prompt_template = "p"

  [rule.selector]
  severity = "WARNING"
"""
    catalog = load_feedback_rules(_write(tmp_path, body))
    with pytest.raises(FeedbackRuleError, match="equal specificity"):
        catalog.match(_signal(project="pest_site_factory", severity=Severity.WARNING))
    # Each rule alone still resolves, so the catalog is not simply broken.
    assert _matched(catalog, _signal(project="other", severity=Severity.WARNING)).rule_id == (
        "by_severity"
    )


def test_a_signal_matching_nothing_returns_none(tmp_path: Path) -> None:
    catalog = load_feedback_rules(_write(tmp_path, _VALID))
    assert catalog.match(_signal(kind=LedgerFactKind.DISPATCH_INTENT_FAILED)) is None


def test_prompt_rendering_uses_signal_fields(tmp_path: Path) -> None:
    catalog = load_feedback_rules(_write(tmp_path, _VALID))
    rendered = catalog.rules[0].render_prompt(_signal())
    assert rendered == "diagnose PROCESS_FAILED"


def test_a_rule_naming_an_unstaffed_tier_is_rejected(tmp_path: Path) -> None:
    """The bench decides who plays which tier; a rule cannot invent one.

    A rule pointing at an unstaffed tier would dispatch into nothing, and the
    only place that is cheap to notice is load time. Nothing else covered this
    guard, so the catalog could name a tier the bench cannot staff and the
    failure would surface as a proposal that quietly goes nowhere.
    """

    body = _VALID.replace('tier = "junior"', 'tier = "staff"')

    def _no_bench(tier: object) -> object:
        raise LookupError(f"no bench for {tier}")

    import local_first_agent_os.monitor_feedback.rules as rules_module

    original = rules_module.resolve_bench
    rules_module.resolve_bench = _no_bench  # type: ignore[assignment]
    try:
        with pytest.raises(FeedbackRuleError, match="unstaffed tier"):
            load_feedback_rules(_write(tmp_path, body))
    finally:
        rules_module.resolve_bench = original  # type: ignore[assignment]
