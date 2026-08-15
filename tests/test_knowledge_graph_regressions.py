# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import inspect

from local_first_agent_os.contracts import (
    ArtifactRole,
    EntityGraph,
    ExtractedEntity,
    GraphConfig,
    ModelRole,
    Ontology,
    WorkspacePolicy,
)
from local_first_agent_os.graph_extraction import (
    MalformedExtractionError,
    apply_ontology,
    parse_extraction_payload,
)
from local_first_agent_os.observability import JsonLogFormatter
from local_first_agent_os.runtime import AppRuntime
from local_first_agent_os.workflow.engine import WorkflowEngine
from local_first_agent_os.workflow.graph import GraphPathOutsidePolicyError


@pytest.mark.parametrize(
    "payload",
    [
        {"relations": []},
        {"entities": {}, "relations": []},
        {"entities": [{"name": "Rahul", "node_type": "Person"}], "relations": []},
    ],
)
def test_extraction_payload_rejects_schema_invalid_structures(
    payload: dict[str, object],
) -> None:
    with pytest.raises(MalformedExtractionError):
        parse_extraction_payload(payload)


def test_ontology_identity_includes_node_type() -> None:
    result = apply_ontology(
        ontology=Ontology(
            entity_types={"Person": "", "Tool": ""},
            relation_types={},
        ),
        max_entities=10,
        entities=[
            ExtractedEntity(name="Atlas", node_type="Person", confidence=0.9),
            ExtractedEntity(name="Atlas", node_type="Tool", confidence=0.9),
        ],
        relations=[],
    )

    assert [(entity.name, entity.node_type) for entity in result.entities] == [
        ("Atlas", "Person"),
        ("Atlas", "Tool"),
    ]


def test_fresh_schema_enforces_graph_endpoint_foreign_keys(runtime: AppRuntime) -> None:
    inspector = inspect(runtime.database.engine)
    edge_foreign_keys = inspector.get_foreign_keys("graph_edges")
    mention_foreign_keys = inspector.get_foreign_keys("graph_node_mentions")

    assert {
        (tuple(foreign_key["constrained_columns"]), foreign_key["referred_table"])
        for foreign_key in edge_foreign_keys
    } >= {
        (("src_node_id",), "graph_nodes"),
        (("dst_node_id",), "graph_nodes"),
    }
    assert {
        (tuple(foreign_key["constrained_columns"]), foreign_key["referred_table"])
        for foreign_key in mention_foreign_keys
    } >= {(("node_id",), "graph_nodes")}


def test_policy_path_check_rejects_a_string_prefix_sibling(
    runtime: AppRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_root = tmp_path / "allowed"
    sibling = tmp_path / "allowed-sibling"
    monkeypatch.setattr(
        runtime.policy_store,
        "all",
        lambda: [
            WorkspacePolicy(
                workspace_id="general",
                root_path=allowed_root,
            )
        ],
    )

    with pytest.raises(GraphPathOutsidePolicyError):
        WorkflowEngine(runtime)._ensure_path_within_policy(sibling)


def test_rebuild_uses_only_the_current_ontology_and_extractor(
    runtime: AppRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = WorkflowEngine(runtime)
    config = GraphConfig(
        ontology=Ontology(
            ontology_version="current",
            entity_types={"Concept": ""},
            relation_types={},
        )
    )
    model_id = runtime.model_manager.registry.resolve_model(ModelRole.GENERAL).model_id
    monkeypatch.setattr(runtime.model_manager, "require_loaded", lambda role: None)
    monkeypatch.setattr(
        runtime.model_manager,
        "embed_texts",
        lambda texts, workflow_id: [[1.0, 0.0] for _ in texts],
    )

    current_source = runtime.artifact_store.write_text(
        role=ArtifactRole.NORMALIZED_TEXT.value,
        text="Current",
        workflow_id=None,
        schema_version="normalized_text.v1",
    )
    stale_source = runtime.artifact_store.write_text(
        role=ArtifactRole.NORMALIZED_TEXT.value,
        text="Stale",
        workflow_id=None,
        schema_version="normalized_text.v1",
    )
    for graph in (
        EntityGraph(
            source_artifact_id=current_source.artifact_id,
            ontology_version="current",
            extractor_model_id=model_id,
            entities=[ExtractedEntity(name="Current", node_type="Concept", confidence=0.9)],
        ),
        EntityGraph(
            source_artifact_id=stale_source.artifact_id,
            ontology_version="stale",
            extractor_model_id=model_id,
            entities=[ExtractedEntity(name="Stale", node_type="Concept", confidence=0.9)],
        ),
    ):
        runtime.artifact_store.write_json(
            role=ArtifactRole.ENTITY_GRAPH.value,
            payload=graph.model_dump(),
            workflow_id=None,
            schema_version=graph.schema_version,
        )

    engine.rebuild_graph(config=config)

    assert [node.canonical_name for node in runtime.repository.list_graph_nodes()] == ["Current"]


def test_structured_formatter_prefers_record_run_identity(runtime: AppRuntime) -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="graph_event",
        args=(),
        exc_info=None,
    )
    record.workflow_id = "graph:test"
    record.workflow_type = "graph_extraction"

    payload = json.loads(JsonLogFormatter(runtime.settings).format(record))

    assert payload["workflow_id"] == "graph:test"
    assert payload["workflow_type"] == "graph_extraction"


def test_graph_node_report_returns_the_match_and_its_neighborhood(
    runtime: AppRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/graph node` is parsed everywhere and executed nowhere.

    The directive feature pins that the subcommand parses and dispatches, but
    nothing runs the report itself, so the body could return an empty match for
    a node that exists and every scenario would still be green.
    """

    engine = WorkflowEngine(runtime)
    config = GraphConfig(
        ontology=Ontology(
            ontology_version="current",
            entity_types={"Concept": ""},
            relation_types={"relates_to": ""},
        )
    )
    monkeypatch.setattr(runtime.model_manager, "require_loaded", lambda role: None)
    monkeypatch.setattr(
        runtime.model_manager,
        "embed_texts",
        lambda texts, workflow_id: [[1.0, 0.0] for _ in texts],
    )
    model_id = runtime.model_manager.registry.resolve_model(ModelRole.GENERAL).model_id
    source = runtime.artifact_store.write_text(
        role=ArtifactRole.NORMALIZED_TEXT.value,
        text="Durable Boundary relates to Saga",
        workflow_id=None,
        schema_version="normalized_text.v1",
    )
    runtime.artifact_store.write_json(
        role=ArtifactRole.ENTITY_GRAPH.value,
        payload=EntityGraph(
            source_artifact_id=source.artifact_id,
            ontology_version="current",
            extractor_model_id=model_id,
            entities=[
                ExtractedEntity(name="Durable Boundary", node_type="Concept", confidence=0.9),
                ExtractedEntity(name="Saga", node_type="Concept", confidence=0.9),
            ],
        ).model_dump(),
        workflow_id=None,
        schema_version=EntityGraph.model_fields["schema_version"].default,
    )
    engine.rebuild_graph(config=config)

    report = engine._graph_node_report("durable   boundary", config)

    assert report["subcommand"] == "node"
    assert report["query"] == "durable   boundary"
    assert [match["canonical_name"] for match in report["matches"]] == ["Durable Boundary"]
    assert "neighborhood" in report


def test_graph_node_report_is_empty_for_an_unknown_name(
    runtime: AppRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown node is an empty report, not an exception."""

    monkeypatch.setattr(runtime.model_manager, "require_loaded", lambda role: None)
    config = GraphConfig(
        ontology=Ontology(
            ontology_version="current", entity_types={"Concept": ""}, relation_types={}
        )
    )

    report = WorkflowEngine(runtime)._graph_node_report("nothing here", config)

    assert report["matches"] == []
