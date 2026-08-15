# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whiteboard perception, corpus match evidence, and snapshot diffing.

The whiteboard and Workflowy are source material. This module produces
durable evidence, never permanent classification:

1. Parsing model output into a typed ``WhiteboardIntentGraph``. Structure
   (groups, ink color, crossed-out marks) is preserved when the model returns
   it; otherwise the loss is recorded as ``FLAT_FALLBACK`` instead of being
   hidden.
2. Attaching corpus match evidence to each board item. Interpretations such
   as novelty are pure functions computed inside whichever workflow needs
   them, so the same evidence can be read differently by different named
   workflows.
3. Diffing consecutive snapshots. An item disappearing next to a surviving
   group is evidence; the diff infers TRANSFERRED or COMPLETED only when the
   corpus supports it and otherwise records UNRESOLVED without demanding a
   label from the operator.

Everything here is deterministic given its inputs; the retrieval searcher is
injected so tests and future backends stay decoupled.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from .contracts import (
    CorpusMatchedItem,
    DisappearanceEvidence,
    DisappearedItem,
    IntentNovelty,
    SearchHit,
    WhiteboardCorpusEvidence,
    WhiteboardDiff,
    WhiteboardExtractionMode,
    WhiteboardIntentGraph,
    WhiteboardIntentGroup,
    WhiteboardIntentItem,
    WorkflowyMatch,
)

WHITEBOARD_EXTRACTION_PROMPT = (
    "You are parsing a photo of a personal whiteboard. Position, ink color, "
    "arrows, and co-location carry meaning: items written together in the "
    "same color form one initiative even when the words do not say so. "
    "Return ONLY a JSON object with this shape and nothing else:\n"
    '{"groups": [{"label": str|null, "ink_color": str|null, '
    '"inferred_project": str|null, "confidence": float, '
    '"items": [{"text": str, "ink_color": str|null, "crossed_out": bool, '
    '"region_hint": str|null, "confidence": float}]}]}\n'
    "Keep item text faithful to what is written. Mark struck-through items "
    "crossed_out=true. Use region_hint for coarse location such as "
    '"top-left". Do not invent items that are not on the board.'
)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DATED_TOP_LEVEL_RE = re.compile(r"^/\d{4}-\d{2}-\d{2}$")

# Searcher takes (query, top_k) and returns corpus hits; production binds
# RetrievalService.fetch_workflowy, tests bind a fake.
WorkflowySearcher = Callable[[str, int], Sequence[SearchHit]]


def _candidate_json_payloads(raw_text: str) -> list[str]:
    candidates: list[str] = []
    fenced = _JSON_FENCE_RE.search(raw_text)
    if fenced is not None:
        candidates.append(fenced.group(1))
    stripped = raw_text.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    return candidates


def _flat_fallback_graph(raw_text: str, source_artifact_id: str) -> WhiteboardIntentGraph:
    lines = [line.strip(" \t-*") for line in raw_text.splitlines()]
    items = [WhiteboardIntentItem(text=line, confidence=0.2) for line in lines if line.strip()]
    groups = [WhiteboardIntentGroup(label=None, confidence=0.2, items=items)] if items else []
    return WhiteboardIntentGraph(
        source_artifact_id=source_artifact_id,
        extraction_mode=WhiteboardExtractionMode.FLAT_FALLBACK,
        groups=groups,
    )


def parse_extraction_output(raw_text: str, source_artifact_id: str) -> WhiteboardIntentGraph:
    """Parse perception-model text into a typed intent graph.

    A parseable ``groups`` payload becomes a STRUCTURED graph. Anything else
    degrades to FLAT_FALLBACK with one low-confidence group per the visible
    lines, so downstream stages always receive the same contract and the
    fidelity loss stays explicit in the data.
    """
    for payload in _candidate_json_payloads(raw_text):
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict) or "groups" not in decoded:
            continue
        try:
            groups = [WhiteboardIntentGroup.model_validate(group) for group in decoded["groups"]]
        except (ValidationError, TypeError):
            continue
        return WhiteboardIntentGraph(
            source_artifact_id=source_artifact_id,
            extraction_mode=WhiteboardExtractionMode.STRUCTURED,
            groups=[group for group in groups if group.items],
        )
    return _flat_fallback_graph(raw_text, source_artifact_id)


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def lexical_overlap(left: str, right: str) -> float:
    """Jaccard overlap over lowercased word tokens, in [0, 1]."""
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union)


@dataclass(frozen=True)
class ReconciliationThresholds:
    """Score boundaries used when a workflow interprets match evidence.

    Combined score is the mean of semantic and lexical similarity so that a
    pure embedding-space neighbor does not merge distinct tasks that merely
    share a project noun, per the shared-entity false-merge failure mode.
    """

    duplicate_score: float = 0.82
    update_score: float = 0.60
    ambiguous_margin: float = 0.05
    pair_score: float = 0.60
    top_k: int = 5

    def __post_init__(self) -> None:
        if not 0 < self.update_score <= self.duplicate_score <= 1:
            raise ValueError("thresholds must satisfy 0 < update_score <= duplicate_score <= 1")


def _combined_score(semantic: float, lexical: float) -> float:
    bounded_semantic = min(max(semantic, 0.0), 1.0)
    return (bounded_semantic + lexical) / 2


def build_corpus_evidence(
    graph: WhiteboardIntentGraph,
    graph_artifact_id: str,
    searcher: WorkflowySearcher,
    thresholds: ReconciliationThresholds | None = None,
) -> WhiteboardCorpusEvidence:
    """Attach scored corpus matches to every board item, without labels."""
    resolved = thresholds or ReconciliationThresholds()
    items: list[CorpusMatchedItem] = []
    for group_index, item_index, item in graph.flattened_items():
        hits = list(searcher(item.text, resolved.top_k))
        matches = sorted(
            (
                WorkflowyMatch(
                    chunk_id=hit.chunk_id,
                    semantic_score=hit.score,
                    lexical_score=lexical_overlap(item.text, hit.text),
                    score=_combined_score(hit.score, lexical_overlap(item.text, hit.text)),
                    text_excerpt=hit.text[:200],
                    metadata=dict(hit.metadata),
                )
                for hit in hits
            ),
            key=lambda match: match.score,
            reverse=True,
        )
        items.append(
            CorpusMatchedItem(
                group_index=group_index,
                item_index=item_index,
                text=item.text,
                crossed_out=item.crossed_out,
                matches=matches[: resolved.top_k],
            )
        )
    return WhiteboardCorpusEvidence(
        graph_artifact_id=graph_artifact_id,
        extraction_mode=graph.extraction_mode,
        items=items,
    )


def classify_novelty(
    matches: Sequence[WorkflowyMatch],
    thresholds: ReconciliationThresholds | None = None,
) -> IntentNovelty:
    """Interpret match evidence on demand; nothing durable is labeled."""
    resolved = thresholds or ReconciliationThresholds()
    if not matches:
        return IntentNovelty.NEW
    best = max(matches, key=lambda match: match.score)
    if best.score >= resolved.duplicate_score:
        return IntentNovelty.DUPLICATE
    if best.score >= resolved.duplicate_score - resolved.ambiguous_margin:
        return IntentNovelty.AMBIGUOUS
    if best.score >= resolved.update_score:
        return IntentNovelty.UPDATE
    return IntentNovelty.NEW


def _disappearance_for(
    text: str,
    searcher: WorkflowySearcher,
    thresholds: ReconciliationThresholds,
    done_top_level: str,
) -> DisappearedItem:
    hits = list(searcher(text, thresholds.top_k))
    best: WorkflowyMatch | None = None
    for hit in hits:
        candidate = WorkflowyMatch(
            chunk_id=hit.chunk_id,
            semantic_score=hit.score,
            lexical_score=lexical_overlap(text, hit.text),
            score=_combined_score(hit.score, lexical_overlap(text, hit.text)),
            text_excerpt=hit.text[:200],
            metadata=dict(hit.metadata),
        )
        if best is None or candidate.score > best.score:
            best = candidate
    if best is None or best.score < thresholds.update_score:
        return DisappearedItem(
            text=text,
            evidence=DisappearanceEvidence.UNRESOLVED,
            rationale="no matching Workflowy change; likely erased or unresolved",
        )
    top_level = best.metadata.get("top_level")
    if top_level == done_top_level:
        return DisappearedItem(
            text=text,
            evidence=DisappearanceEvidence.COMPLETED,
            best_match_chunk_id=best.chunk_id,
            best_match_top_level=top_level,
            rationale=f"matching item under {top_level} scored {best.score:.2f}",
        )
    if isinstance(top_level, str) and (
        _DATED_TOP_LEVEL_RE.match(top_level) or top_level == "/tomorrow"
    ):
        return DisappearedItem(
            text=text,
            evidence=DisappearanceEvidence.TRANSFERRED,
            best_match_chunk_id=best.chunk_id,
            best_match_top_level=top_level,
            rationale=f"matching item under daily view {top_level} scored {best.score:.2f}",
        )
    return DisappearedItem(
        text=text,
        evidence=DisappearanceEvidence.UNRESOLVED,
        best_match_chunk_id=best.chunk_id,
        best_match_top_level=top_level if isinstance(top_level, str) else None,
        rationale="corpus match exists but its location is not strong evidence",
    )


def diff_graphs(
    previous: WhiteboardIntentGraph,
    previous_artifact_id: str,
    current: WhiteboardIntentGraph,
    current_artifact_id: str,
    searcher: WorkflowySearcher,
    thresholds: ReconciliationThresholds | None = None,
    done_top_level: str = "/done",
) -> WhiteboardDiff:
    """Compare consecutive snapshots and classify what left the board.

    Items are paired across snapshots by lexical overlap so small rewordings
    survive. Newly crossed-out items count as disappeared: striking through is
    the board's own completion mark.
    """
    resolved = thresholds or ReconciliationThresholds()
    previous_items = [item for _, _, item in previous.flattened_items()]
    current_items = [item for _, _, item in current.flattened_items()]
    current_active = [item.text for item in current_items if not item.crossed_out]

    def _paired(text: str, pool: Sequence[str]) -> bool:
        return any(lexical_overlap(text, candidate) >= resolved.pair_score for candidate in pool)

    appeared = [
        item.text
        for item in current_items
        if not item.crossed_out and not _paired(item.text, [prior.text for prior in previous_items])
    ]
    persisted = [
        item.text
        for item in previous_items
        if not item.crossed_out and _paired(item.text, current_active)
    ]
    disappeared = [
        _disappearance_for(item.text, searcher, resolved, done_top_level)
        for item in previous_items
        if not item.crossed_out and not _paired(item.text, current_active)
    ]
    return WhiteboardDiff(
        previous_graph_artifact_id=previous_artifact_id,
        current_graph_artifact_id=current_artifact_id,
        appeared=appeared,
        persisted=persisted,
        disappeared=disappeared,
    )
