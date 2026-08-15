# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The operator-owned rule catalog: which signals may propose work.

Rules are data, not code.  Deciding that a failing pest milestone deserves a
junior diagnosis task is policy, and policy that requires a deploy to change is
policy nobody changes.

Every problem in this file is an operator or programmer error and crashes at
load.  A severe-log here would leave the reactor running against a catalog it
silently misread, which is the one failure mode a feedback loop cannot survive:
being quietly off looks exactly like having nothing to report.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..coordination.outcomes import FailureCategory
from ..staffing import Tier, resolve_bench
from .signals import LedgerFactKind, MonitorSignal, Severity

CATALOG_SCHEMA_VERSION = "feedback_rules.v1"


class FeedbackRuleError(ValueError):
    """An invalid or ambiguous rule catalog. Always fatal."""


class FeedbackResponse(StrEnum):
    """What a matched rule is allowed to do.

    Phase 1 ships the two responses that change nothing: an advisory intent is
    read-only and takes no worktree, and a digest entry is an artifact.
    ``code_proposal`` arrives in Phase 3 together with its ``FEEDBACK_CODE``
    approval type, because a response with no handler is a rule that silently
    does nothing.
    """

    ADVISORY = "advisory"
    DIGEST_ONLY = "digest_only"


@dataclass(frozen=True, slots=True)
class RuleSelector:
    """A conjunction over signal fields. ``None`` means "any".

    Specificity is the count of bound fields, which is what makes
    most-specific-wins a total order over matches rather than a tiebreak.
    """

    signal_kind: LedgerFactKind | None = None
    target_project_id: str | None = None
    severity: Severity | None = None
    failure_category: FailureCategory | None = None

    # One ordered field list, used by specificity, matching, and the payload.
    # Three copies of "which fields are selectable" is three places to forget
    # a field when a fifth one is added; the count and the comparison must
    # agree or most-specific-wins silently stops being most-specific.
    _FIELD_NAMES = ("signal_kind", "target_project_id", "severity", "failure_category")

    def _bindings(self) -> tuple[StrEnum | str | None, ...]:
        # Every selectable field is a StrEnum or a plain string; the union is
        # what lets to_payload stay honest about returning strings.
        return tuple(getattr(self, name) for name in self._FIELD_NAMES)

    @property
    def specificity(self) -> int:
        return sum(1 for binding in self._bindings() if binding is not None)

    def matches(self, signal: MonitorSignal) -> bool:
        observed = (
            signal.kind,
            signal.target_project_id,
            signal.severity,
            signal.failure_category,
        )
        return all(
            expected is None or actual == expected
            for expected, actual in zip(self._bindings(), observed, strict=True)
        )

    def to_payload(self) -> dict[str, str | None]:
        return {
            name: (binding.value if isinstance(binding, StrEnum) else binding)
            for name, binding in zip(self._FIELD_NAMES, self._bindings(), strict=True)
        }


@dataclass(frozen=True, slots=True)
class FeedbackRule:
    """One catalog entry: a selector, a bounded response, and its budgets."""

    rule_id: str
    selector: RuleSelector
    response: FeedbackResponse
    tier: Tier
    cooldown_seconds: float
    daily_cap: int
    prompt_template: str
    enabled: bool = True

    def render_prompt(self, signal: MonitorSignal) -> str:
        return self.prompt_template.format(
            kind=signal.kind.value,
            fingerprint=signal.fingerprint,
            error_code=signal.error_code or "unknown",
            summary=signal.summary,
            target_project_id=signal.target_project_id or "unspecified",
            evidence_table=signal.evidence.table,
            evidence_row_id=signal.evidence.row_id,
        )


@dataclass(frozen=True, slots=True)
class FeedbackRuleCatalog:
    """The loaded catalog. Only enabled rules are ever matched."""

    rules: tuple[FeedbackRule, ...]
    global_open_intent_cap: int
    source_path: str

    def match(self, signal: MonitorSignal) -> FeedbackRule | None:
        """Return the most specific enabled rule matching ``signal``.

        An ambiguous match is a crash rather than a tiebreak.  Load-time
        validation rejects *identical* selectors, but two different selectors
        of equal specificity can still both match one signal, and that is only
        knowable once the signal exists.  Picking one arbitrarily would make
        the loop's behavior depend on file order.
        """

        matched = [rule for rule in self.rules if rule.enabled and rule.selector.matches(signal)]
        if not matched:
            return None
        best = max(rule.selector.specificity for rule in matched)
        finalists = [rule for rule in matched if rule.selector.specificity == best]
        if len(finalists) > 1:
            raise FeedbackRuleError(
                f"signal {signal.fingerprint} ({signal.kind.value}) matches "
                f"{len(finalists)} rules of equal specificity {best}: "
                f"{sorted(rule.rule_id for rule in finalists)}. "
                "Make one selector more specific, or disable one rule."
            )
        return finalists[0]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise FeedbackRuleError(message)


def _enum_member[T: StrEnum](enum: type[T], raw: object, field: str, rule_id: str) -> T:
    try:
        return enum(str(raw))
    except ValueError:
        valid = sorted(member.value for member in enum)
        raise FeedbackRuleError(
            f"rule '{rule_id}' has invalid {field} {raw!r}; valid values are {valid}"
        ) from None


def _build_selector(raw: dict[str, Any], rule_id: str) -> RuleSelector:
    unknown = set(raw) - {"signal_kind", "target_project_id", "severity", "failure_category"}
    _require(
        not unknown,
        f"rule '{rule_id}' selector has unknown keys {sorted(unknown)}",
    )
    project = raw.get("target_project_id")
    _require(
        project is None or (isinstance(project, str) and project.strip() != ""),
        f"rule '{rule_id}' selector target_project_id must be a non-empty string when set",
    )
    return RuleSelector(
        signal_kind=(
            _enum_member(LedgerFactKind, raw["signal_kind"], "selector.signal_kind", rule_id)
            if "signal_kind" in raw
            else None
        ),
        target_project_id=project,
        severity=(
            _enum_member(Severity, raw["severity"], "selector.severity", rule_id)
            if "severity" in raw
            else None
        ),
        failure_category=(
            _enum_member(
                FailureCategory, raw["failure_category"], "selector.failure_category", rule_id
            )
            if "failure_category" in raw
            else None
        ),
    )


def _build_rule(raw: dict[str, Any]) -> FeedbackRule:
    rule_id = str(raw.get("rule_id", "")).strip()
    _require(bool(rule_id), "every rule needs a non-empty rule_id")
    unknown = set(raw) - {
        "rule_id",
        "enabled",
        "response",
        "tier",
        "cooldown_seconds",
        "daily_cap",
        "prompt_template",
        "selector",
    }
    _require(not unknown, f"rule '{rule_id}' has unknown keys {sorted(unknown)}")

    response = _enum_member(FeedbackResponse, raw.get("response"), "response", rule_id)
    tier = _enum_member(Tier, raw.get("tier"), "tier", rule_id)
    try:
        resolve_bench(tier)
    except Exception as exc:
        # The bench is the single source of truth for who plays which tier.
        # A rule naming an unstaffed tier would dispatch into nothing.
        raise FeedbackRuleError(f"rule '{rule_id}' names unstaffed tier '{tier.value}'") from exc

    cooldown = raw.get("cooldown_seconds", 0)
    _require(
        isinstance(cooldown, int | float) and not isinstance(cooldown, bool) and cooldown >= 0,
        f"rule '{rule_id}' cooldown_seconds must be a number >= 0",
    )
    daily_cap = raw.get("daily_cap", 0)
    _require(
        isinstance(daily_cap, int) and not isinstance(daily_cap, bool) and daily_cap >= 1,
        f"rule '{rule_id}' daily_cap must be an integer >= 1; a cap of 0 is a disabled rule, "
        "which 'enabled = false' already says more clearly",
    )

    prompt_template = str(raw.get("prompt_template", "")).strip()
    _require(
        response is not FeedbackResponse.ADVISORY or bool(prompt_template),
        f"rule '{rule_id}' has response 'advisory' and no prompt_template; "
        "an advisory intent with no prompt dispatches an agent with no task",
    )
    rule = FeedbackRule(
        rule_id=rule_id,
        selector=_build_selector(dict(raw.get("selector", {})), rule_id),
        response=response,
        tier=tier,
        cooldown_seconds=float(cooldown),
        daily_cap=int(daily_cap),
        prompt_template=prompt_template,
        enabled=bool(raw.get("enabled", True)),
    )
    if prompt_template:
        _validate_prompt_template(rule)
    return rule


_PROMPT_FIELDS = {
    "kind",
    "fingerprint",
    "error_code",
    "summary",
    "target_project_id",
    "evidence_table",
    "evidence_row_id",
}


def _validate_prompt_template(rule: FeedbackRule) -> None:
    """Reject a template at load rather than at dispatch.

    A ``KeyError`` raised while rendering would surface hours later, inside a
    cycle, as a failure to propose work nobody asked for.
    """

    try:
        rule.prompt_template.format(**dict.fromkeys(_PROMPT_FIELDS, ""))
    except (KeyError, IndexError, ValueError) as exc:
        raise FeedbackRuleError(
            f"rule '{rule.rule_id}' prompt_template is not renderable ({exc}); "
            f"available fields are {sorted(_PROMPT_FIELDS)}"
        ) from None


def load_feedback_rules(path: Path) -> FeedbackRuleCatalog:
    """Load and fully validate the catalog, or raise ``FeedbackRuleError``."""

    if not path.exists():
        raise FeedbackRuleError(f"feedback rule catalog not found at {path}")
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise FeedbackRuleError(
            f"feedback rule catalog at {path} is not valid TOML: {exc}"
        ) from exc

    version = str(document.get("schema_version", ""))
    _require(
        version == CATALOG_SCHEMA_VERSION,
        f"feedback rule catalog at {path} declares schema_version {version!r}; "
        f"this build reads {CATALOG_SCHEMA_VERSION!r}",
    )
    global_cap = document.get("global_open_intent_cap", 0)
    _require(
        isinstance(global_cap, int) and not isinstance(global_cap, bool) and global_cap >= 1,
        f"feedback rule catalog at {path} needs global_open_intent_cap >= 1",
    )

    rules = tuple(_build_rule(dict(entry)) for entry in document.get("rule", []))
    _require(bool(rules), f"feedback rule catalog at {path} defines no rules")

    seen_ids: set[str] = set()
    for rule in rules:
        _require(rule.rule_id not in seen_ids, f"duplicate rule_id '{rule.rule_id}'")
        seen_ids.add(rule.rule_id)

    # Identical selectors are unresolvable no matter which signal arrives, so
    # they are knowable now. Equal-specificity-but-different selectors can only
    # be caught when a signal matches both; ``FeedbackRuleCatalog.match`` does.
    by_selector: dict[RuleSelector, str] = {}
    for rule in rules:
        if not rule.enabled:
            continue
        clash = by_selector.get(rule.selector)
        _require(
            clash is None,
            f"rules '{clash}' and '{rule.rule_id}' have identical selectors "
            f"{rule.selector.to_payload()}; no signal could ever choose between them",
        )
        by_selector[rule.selector] = rule.rule_id

    return FeedbackRuleCatalog(
        rules=rules,
        global_open_intent_cap=int(global_cap),
        source_path=str(path),
    )
