# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The create-tomorrow named workflow's pure interpretation logic.

Concepts like routine, primary goal, and maintenance exist only inside one
invocation of this workflow. The sources (whiteboard evidence, corpus
matches, the stored regimen) are interpreted in the context of the user's
specific instruction, and the output is a temporary daily execution view:
a ``DailyViewPatch`` targeting one dated top-level Workflowy node.

A model interprets the instruction when it returns parseable structure;
otherwise a deterministic skeleton stands in and the degradation is recorded
as ``FALLBACK_SKELETON``. Nothing here writes to Workflowy.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from pydantic import BaseModel, Field, ValidationError

from .contracts import (
    DailyViewPatch,
    IntentNovelty,
    InterpretationMode,
    WhiteboardCorpusEvidence,
    WorkflowyOutlineNode,
)
from .settings import Settings
from .whiteboard_intent import ReconciliationThresholds, classify_novelty

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class RegimenConfig(BaseModel):
    """The stored, reusable regimen; a source, not a planner."""

    name: str = "default"
    morning: list[str] = Field(
        default_factory=lambda: [
            "Medication and breakfast",
            "Review today's target list",
        ]
    )
    maintenance: list[str] = Field(
        default_factory=lambda: [
            "Sync and review whiteboard changes",
            "Update the agent handoff",
        ]
    )
    max_primary_items: int = Field(default=3, ge=1)
    done_top_level: str = "/done"


def load_regimen(settings: Settings) -> RegimenConfig:
    """Load the regimen TOML; absent file or table means explicit defaults."""
    data = settings.load_toml(settings.regimen_path)
    return RegimenConfig.model_validate(data.get("regimen", {}))


def default_target_top_level(today: date | None = None) -> str:
    """Dated top-level label, e.g. ``/2026-07-15``.

    The dated form is preferred over ``/tomorrow`` so historical evidence of
    intent survives each daily replacement.
    """
    anchor = today or date.today()
    return f"/{(anchor + timedelta(days=1)).isoformat()}"


def select_primary_candidates(
    evidence: WhiteboardCorpusEvidence | None,
    regimen: RegimenConfig,
    thresholds: ReconciliationThresholds | None = None,
) -> list[str]:
    """Pick board items worth proposing, using evidence-derived interpretation.

    Crossed-out items are the board's own completion mark. Duplicates (per the
    on-demand novelty interpretation) already live in Workflowy and belong to
    that view rather than a new bullet. Board order is preserved; the regimen
    caps how many items a one-day view may carry.
    """
    if evidence is None:
        return []
    selected: list[str] = []
    for item in evidence.items:
        if item.crossed_out:
            continue
        if classify_novelty(item.matches, thresholds) == IntentNovelty.DUPLICATE:
            continue
        selected.append(item.text)
        if len(selected) >= regimen.max_primary_items:
            break
    return selected


def build_interpretation_prompt(
    instruction: str,
    candidates: list[str],
    regimen: RegimenConfig,
) -> str:
    """Bounded prompt asking the model to interpret one specific instruction."""
    candidate_lines = "\n".join(f"- {text}" for text in candidates) or "- (none)"
    morning = "\n".join(f"- {text}" for text in regimen.morning)
    maintenance = "\n".join(f"- {text}" for text in regimen.maintenance)
    return (
        "You are building tomorrow's daily execution view for one person.\n"
        f"Their instruction: {instruction}\n\n"
        "Whiteboard items already screened against their Workflowy corpus:\n"
        f"{candidate_lines}\n\n"
        f"Stored morning regimen:\n{morning}\n\n"
        f"Stored maintenance regimen:\n{maintenance}\n\n"
        "Return ONLY a JSON object of this shape and nothing else:\n"
        '{"sections": [{"text": str, "children": [{"text": str, '
        '"children": [...]}]}]}\n'
        "Use the instruction to decide what belongs; do not invent tasks that "
        "are not in the inputs. Keep it small enough to read on a phone."
    )


def _parse_nodes(raw_nodes: object) -> list[WorkflowyOutlineNode]:
    if not isinstance(raw_nodes, list):
        raise TypeError("outline nodes must be a list")
    nodes: list[WorkflowyOutlineNode] = []
    for raw in raw_nodes:
        if isinstance(raw, str):
            nodes.append(WorkflowyOutlineNode(text=raw))
            continue
        node = WorkflowyOutlineNode.model_validate(
            {**raw, "children": []} if isinstance(raw, dict) else raw
        )
        children = raw.get("children", []) if isinstance(raw, dict) else []
        node = node.model_copy(update={"children": _parse_nodes(children)})
        nodes.append(node)
    return nodes


def parse_outline_output(raw_text: str) -> list[WorkflowyOutlineNode] | None:
    """Parse model output into outline sections, or None to trigger fallback."""
    candidates: list[str] = []
    fenced = _JSON_FENCE_RE.search(raw_text)
    if fenced is not None:
        candidates.append(fenced.group(1))
    stripped = raw_text.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    for payload in candidates:
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict) or "sections" not in decoded:
            continue
        try:
            sections = _parse_nodes(decoded["sections"])
        except (ValidationError, TypeError):
            continue
        if sections:
            return sections
    return None


def build_fallback_skeleton(
    candidates: list[str],
    regimen: RegimenConfig,
) -> list[WorkflowyOutlineNode]:
    """Deterministic daily view when no model interpretation is available."""
    sections = [
        WorkflowyOutlineNode(
            text="Morning",
            children=[WorkflowyOutlineNode(text=text) for text in regimen.morning],
        )
    ]
    if candidates:
        sections.append(
            WorkflowyOutlineNode(
                text="Primary",
                children=[WorkflowyOutlineNode(text=text) for text in candidates],
            )
        )
    sections.append(
        WorkflowyOutlineNode(
            text="Maintenance",
            children=[WorkflowyOutlineNode(text=text) for text in regimen.maintenance],
        )
    )
    return sections


def build_daily_view_patch(
    *,
    instruction: str,
    model_output_text: str | None,
    candidates: list[str],
    regimen: RegimenConfig,
    target_top_level: str,
    evidence_artifact_id: str | None,
    diff_artifact_id: str | None,
) -> DailyViewPatch:
    """Assemble the approval-required patch from one invocation's inputs."""
    notes: list[str] = []
    sections: list[WorkflowyOutlineNode] | None = None
    mode = InterpretationMode.FALLBACK_SKELETON
    if model_output_text is not None:
        sections = parse_outline_output(model_output_text)
        if sections is not None:
            mode = InterpretationMode.MODEL_STRUCTURED
    if sections is None:
        sections = build_fallback_skeleton(candidates, regimen)
        notes.append("model interpretation unavailable; deterministic skeleton stands in")
    if not candidates:
        notes.append("no whiteboard candidates survived screening")
    return DailyViewPatch(
        instruction=instruction,
        target_top_level=target_top_level,
        interpretation_mode=mode,
        sections=sections,
        evidence_artifact_id=evidence_artifact_id,
        diff_artifact_id=diff_artifact_id,
        notes=notes,
    )
