# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_first_agent_os.contracts import (
    CorpusMatchedItem,
    DisappearanceEvidence,
    IntentNovelty,
    SearchHit,
    SourceType,
    WhiteboardCorpusEvidence,
    WhiteboardExtractionMode,
    WhiteboardIntentGraph,
    WhiteboardIntentGroup,
    WhiteboardIntentItem,
    WorkflowStatus,
    WorkflowType,
    WorkflowyMatch,
    WorkspaceId,
)
from local_first_agent_os.create_tomorrow import (
    RegimenConfig,
    build_daily_view_patch,
    default_target_top_level,
    parse_outline_output,
    select_primary_candidates,
)
from local_first_agent_os.ingress import normalize_file_event, normalize_scheduled_event
from local_first_agent_os.whiteboard_intent import (
    ReconciliationThresholds,
    build_corpus_evidence,
    classify_novelty,
    diff_graphs,
    lexical_overlap,
    parse_extraction_output,
)
from local_first_agent_os.workflow import WorkflowEngine

STRUCTURED_PAYLOAD = {
    "groups": [
        {
            "label": "Gemma 4 privileged agent",
            "ink_color": "blue",
            "inferred_project": "agent-os",
            "confidence": 0.9,
            "items": [
                {
                    "text": "build capability broker interface",
                    "ink_color": "blue",
                    "crossed_out": False,
                    "region_hint": "top-left",
                    "confidence": 0.85,
                },
                {
                    "text": "define secret boundaries",
                    "ink_color": "blue",
                    "crossed_out": True,
                    "region_hint": "top-left",
                    "confidence": 0.8,
                },
            ],
        }
    ]
}


def _hit(chunk_id: str, text: str, score: float, top_level: str | None = None) -> SearchHit:
    metadata: dict[str, object] = {}
    if top_level is not None:
        metadata["top_level"] = top_level
    return SearchHit(
        chunk_id=chunk_id,
        artifact_id="artifact-1",
        workspace_id=WorkspaceId.WORKFLOWY.value,
        text=text,
        score=score,
        metadata=metadata,
    )


def _graph(texts: list[str], crossed: set[str] | None = None) -> WhiteboardIntentGraph:
    crossed = crossed or set()
    return WhiteboardIntentGraph(
        source_artifact_id="src-artifact",
        extraction_mode=WhiteboardExtractionMode.STRUCTURED,
        groups=[
            WhiteboardIntentGroup(
                label=None,
                items=[
                    WhiteboardIntentItem(text=text, crossed_out=text in crossed, confidence=0.9)
                    for text in texts
                ],
            )
        ],
    )


def test_parse_structured_json_preserves_groups_and_marks() -> None:
    graph = parse_extraction_output(json.dumps(STRUCTURED_PAYLOAD), "src-artifact")
    assert graph.extraction_mode == WhiteboardExtractionMode.STRUCTURED
    assert len(graph.groups) == 1
    group = graph.groups[0]
    assert group.label == "Gemma 4 privileged agent"
    assert group.items[0].region_hint == "top-left"
    assert group.items[1].crossed_out is True


def test_parse_fenced_json_is_structured() -> None:
    fenced = "Here you go:\n```json\n" + json.dumps(STRUCTURED_PAYLOAD) + "\n```"
    graph = parse_extraction_output(fenced, "src-artifact")
    assert graph.extraction_mode == WhiteboardExtractionMode.STRUCTURED
    assert graph.groups[0].items[0].text == "build capability broker interface"


def test_parse_flat_text_falls_back_visibly() -> None:
    graph = parse_extraction_output("- buy milk\n- call dentist\n", "src-artifact")
    assert graph.extraction_mode == WhiteboardExtractionMode.FLAT_FALLBACK
    assert [item.text for _, _, item in graph.flattened_items()] == [
        "buy milk",
        "call dentist",
    ]


def test_parse_invalid_group_payload_falls_back() -> None:
    bad = json.dumps({"groups": [{"items": [{"text": 42}]}]})
    graph = parse_extraction_output(bad, "src-artifact")
    assert graph.extraction_mode == WhiteboardExtractionMode.FLAT_FALLBACK


def test_lexical_overlap_bounds() -> None:
    assert lexical_overlap("build broker", "build broker") == 1.0
    assert lexical_overlap("build broker", "unrelated words") == 0.0
    assert 0 < lexical_overlap("build the broker", "build a broker now") < 1


def test_corpus_evidence_stores_matches_without_labels() -> None:
    graph = _graph(["build capability broker interface"])
    evidence = build_corpus_evidence(
        graph,
        "graph-artifact",
        searcher=lambda query, top_k: [_hit("chunk-dup", "build capability broker interface", 0.9)],
    )
    item = evidence.items[0]
    assert item.matches[0].chunk_id == "chunk-dup"
    assert not hasattr(item, "novelty")
    assert not hasattr(item, "kind")


def test_classify_novelty_is_an_on_demand_interpretation() -> None:
    def match(score: float) -> WorkflowyMatch:
        return WorkflowyMatch(
            chunk_id="chunk-x",
            score=score,
            semantic_score=score,
            lexical_score=score,
            text_excerpt="text",
        )

    assert classify_novelty([]) == IntentNovelty.NEW
    assert classify_novelty([match(0.95)]) == IntentNovelty.DUPLICATE
    assert classify_novelty([match(0.79)]) == IntentNovelty.AMBIGUOUS
    assert classify_novelty([match(0.65)]) == IntentNovelty.UPDATE
    assert classify_novelty([match(0.30)]) == IntentNovelty.NEW


def test_shared_project_noun_alone_is_not_a_duplicate() -> None:
    graph = _graph(["build gemma privileged agent broker"])
    evidence = build_corpus_evidence(
        graph,
        "graph-artifact",
        searcher=lambda query, top_k: [
            # Semantically hot neighbor that only shares the project noun.
            _hit("chunk-old", "gemma benchmark latency results from last month", 0.95)
        ],
    )
    novelty = classify_novelty(evidence.items[0].matches)
    assert novelty != IntentNovelty.DUPLICATE


def test_thresholds_reject_inverted_bounds() -> None:
    with pytest.raises(ValueError):
        ReconciliationThresholds(duplicate_score=0.5, update_score=0.8)


def test_diff_infers_completed_from_done_top_level() -> None:
    previous = _graph(["ship the broker spec", "call the dentist"])
    current = _graph(["call the dentist"])
    diff = diff_graphs(
        previous,
        "prev-artifact",
        current,
        "curr-artifact",
        searcher=lambda query, top_k: [
            _hit("chunk-done", "ship the broker spec", 0.9, top_level="/done")
        ],
    )
    assert diff.persisted == ["call the dentist"]
    assert len(diff.disappeared) == 1
    gone = diff.disappeared[0]
    assert gone.evidence == DisappearanceEvidence.COMPLETED
    assert gone.best_match_top_level == "/done"


def test_diff_infers_transferred_from_dated_view() -> None:
    previous = _graph(["test gemma as junior planner"])
    current = _graph([])
    diff = diff_graphs(
        previous,
        "prev-artifact",
        current,
        "curr-artifact",
        searcher=lambda query, top_k: [
            _hit(
                "chunk-day",
                "test gemma as junior planner",
                0.9,
                top_level="/2026-07-15",
            )
        ],
    )
    assert diff.disappeared[0].evidence == DisappearanceEvidence.TRANSFERRED


def test_diff_leaves_weak_evidence_unresolved() -> None:
    previous = _graph(["a fleeting shower thought"])
    current = _graph([])
    diff = diff_graphs(
        previous,
        "prev-artifact",
        current,
        "curr-artifact",
        searcher=lambda query, top_k: [],
    )
    assert diff.disappeared[0].evidence == DisappearanceEvidence.UNRESOLVED


def test_diff_pairs_reworded_items_and_finds_appeared() -> None:
    previous = _graph(["build the capability broker interface"])
    current = _graph(["build capability broker interface", "entirely new board idea"])
    diff = diff_graphs(
        previous,
        "prev-artifact",
        current,
        "curr-artifact",
        searcher=lambda query, top_k: [],
    )
    assert diff.persisted == ["build the capability broker interface"]
    assert diff.appeared == ["entirely new board idea"]
    assert diff.disappeared == []


def test_default_target_top_level_is_dated() -> None:
    from datetime import date

    assert default_target_top_level(date(2026, 7, 14)) == "/2026-07-15"


def _build_test_corpus_evidence(items: list[CorpusMatchedItem]) -> WhiteboardCorpusEvidence:
    return WhiteboardCorpusEvidence(
        graph_artifact_id="graph-artifact",
        extraction_mode=WhiteboardExtractionMode.STRUCTURED,
        items=items,
    )


def _matched_item(
    text: str, index: int, score: float | None = None, crossed_out: bool = False
) -> CorpusMatchedItem:
    matches = []
    if score is not None:
        matches = [
            WorkflowyMatch(
                chunk_id="chunk-x",
                score=score,
                semantic_score=score,
                lexical_score=score,
                text_excerpt=text,
            )
        ]
    return CorpusMatchedItem(
        group_index=0,
        item_index=index,
        text=text,
        crossed_out=crossed_out,
        matches=matches,
    )


def test_select_candidates_screens_duplicates_and_crossed_out() -> None:
    regimen = RegimenConfig(max_primary_items=3)
    evidence = _build_test_corpus_evidence(
        [
            _matched_item("already in workflowy", 0, score=0.95),
            _matched_item("struck through on the board", 1, crossed_out=True),
            _matched_item("write broker spec", 2),
            _matched_item("test gemma junior planner", 3, score=0.65),
        ]
    )
    assert select_primary_candidates(evidence, regimen) == [
        "write broker spec",
        "test gemma junior planner",
    ]


def test_select_candidates_respects_regimen_cap_and_board_order() -> None:
    regimen = RegimenConfig(max_primary_items=2)
    evidence = _build_test_corpus_evidence(
        [_matched_item(f"task {index}", index) for index in range(4)]
    )
    assert select_primary_candidates(evidence, regimen) == ["task 0", "task 1"]


def test_parse_outline_output_accepts_nested_sections() -> None:
    payload = {
        "sections": [
            {
                "text": "Primary",
                "children": [
                    {
                        "text": "Implement the privileged capability interface",
                        "children": ["Define allowed operations"],
                    }
                ],
            }
        ]
    }
    sections = parse_outline_output(json.dumps(payload))
    assert sections is not None
    assert sections[0].children[0].children[0].text == "Define allowed operations"


def test_parse_outline_output_rejects_prose() -> None:
    assert parse_outline_output("Sure! Here's a plan for tomorrow...") is None


def test_patch_falls_back_to_skeleton_and_records_it() -> None:
    regimen = RegimenConfig()
    patch = build_daily_view_patch(
        instruction="add whiteboard items and my regimen",
        model_output_text="not json at all",
        candidates=["write broker spec"],
        regimen=regimen,
        target_top_level="/2026-07-15",
        evidence_artifact_id="evidence-artifact",
        diff_artifact_id=None,
    )
    assert patch.interpretation_mode.value == "fallback_skeleton"
    section_labels = [section.text for section in patch.sections]
    assert section_labels == ["Morning", "Primary", "Maintenance"]
    primary = patch.sections[1]
    assert [node.text for node in primary.children] == ["write broker spec"]
    assert patch.requires_approval is True
    assert any("skeleton" in note for note in patch.notes)


def test_patch_uses_model_structure_when_parseable() -> None:
    regimen = RegimenConfig()
    model_output = json.dumps({"sections": [{"text": "Primary", "children": ["one bounded task"]}]})
    patch = build_daily_view_patch(
        instruction="only the gemma cluster",
        model_output_text=model_output,
        candidates=["one bounded task"],
        regimen=regimen,
        target_top_level="/2026-07-15",
        evidence_artifact_id=None,
        diff_artifact_id=None,
    )
    assert patch.interpretation_mode.value == "model_structured"
    assert patch.sections[0].children[0].text == "one bounded task"


def _fixture_workspace_root(runtime, workspace_id: str, root: Path) -> None:
    policy = runtime.policy_store.get(workspace_id)
    runtime.policy_store._policies[workspace_id] = policy.model_copy(  # type: ignore[attr-defined]
        update={"root_path": root}
    )


def _execute_whiteboard_intent_workflow(runtime, workspace_root: Path, name: str = "board.png"):
    image = workspace_root / name
    image.write_bytes(b"\x89PNG\r\n\x1a\nmock-bytes" + name.encode())
    event = normalize_file_event(
        path=image,
        workspace_id=WorkspaceId.WHITEBOARD_OCR.value,
        workflow_type=WorkflowType.WHITEBOARD_INTENT,
    )
    return WorkflowEngine(runtime).whiteboard_intent(event)


def test_whiteboard_intent_workflow_persists_typed_build_test_corpus_evidence(
    runtime, tmp_path: Path
) -> None:
    workspace_root = tmp_path / "wb"
    workspace_root.mkdir()
    _fixture_workspace_root(runtime, WorkspaceId.WHITEBOARD_OCR.value, workspace_root)
    result = _execute_whiteboard_intent_workflow(runtime, workspace_root)
    assert result.status == WorkflowStatus.COMPLETED
    roles = {str(artifact.role) for artifact in result.artifacts}
    assert {
        "source_image",
        "whiteboard_intent_graph",
        "whiteboard_corpus_evidence",
    }.issubset(roles)
    graph_artifact = next(
        artifact for artifact in result.artifacts if str(artifact.role) == "whiteboard_intent_graph"
    )
    graph = WhiteboardIntentGraph.model_validate(
        runtime.artifact_store.read_json(graph_artifact.artifact_id)
    )
    # Mock OCR emits flat prose, so the typed fallback must be recorded.
    assert graph.extraction_mode == WhiteboardExtractionMode.FLAT_FALLBACK


def test_second_snapshot_produces_a_diff_artifact(runtime, tmp_path: Path) -> None:
    workspace_root = tmp_path / "wb"
    workspace_root.mkdir()
    _fixture_workspace_root(runtime, WorkspaceId.WHITEBOARD_OCR.value, workspace_root)
    first = _execute_whiteboard_intent_workflow(runtime, workspace_root, "board-one.png")
    assert all(str(a.role) != "whiteboard_diff" for a in first.artifacts)
    second = _execute_whiteboard_intent_workflow(runtime, workspace_root, "board-two.png")
    assert any(str(a.role) == "whiteboard_diff" for a in second.artifacts)


def test_create_tomorrow_requires_an_instruction(runtime) -> None:
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="create.tomorrow",
        payload={},
    )
    with pytest.raises(ValueError, match="instruction"):
        WorkflowEngine(runtime).create_tomorrow(event)


def test_create_tomorrow_emits_dated_patch_in_manual_review(runtime, tmp_path: Path) -> None:
    workspace_root = tmp_path / "wb"
    workspace_root.mkdir()
    _fixture_workspace_root(runtime, WorkspaceId.WHITEBOARD_OCR.value, workspace_root)
    _execute_whiteboard_intent_workflow(runtime, workspace_root)
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="create.tomorrow",
        payload={
            "instruction": (
                "Add some things from my whiteboard that I should be working on "
                "and put it under a top-level bullet"
            ),
            "target_top_level": "/2026-07-15",
        },
    )
    result = WorkflowEngine(runtime).create_tomorrow(event)
    assert result.status == WorkflowStatus.MANUAL_REVIEW
    assert result.manual_review_reason is not None
    patch_artifact = next(
        artifact for artifact in result.artifacts if str(artifact.role) == "daily_view_patch"
    )
    payload = runtime.artifact_store.read_json(patch_artifact.artifact_id)
    assert payload["requires_approval"] is True
    assert payload["target_top_level"] == "/2026-07-15"
    # Mock models return prose, so the deterministic skeleton stands in.
    assert payload["interpretation_mode"] == "fallback_skeleton"
    section_labels = [section["text"] for section in payload["sections"]]
    assert "Morning" in section_labels
    assert "Maintenance" in section_labels
