# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from prometheus_client.metrics import MetricWrapperBase
from pytest_bdd import given, parsers, scenarios, then, when

from local_first_agent_os.contracts import (
    ArtifactRef,
    ArtifactRole,
    DirectiveSpec,
    EntityGraph,
    GraphConfig,
    GraphExtractionBounds,
    GraphNeighborhood,
    GraphRetrievalBounds,
    IngressEvent,
    ModelRole,
    Ontology,
    SearchHit,
    SourceType,
    WorkflowResult,
    WorkflowStatus,
    WorkspaceId,
    WorkspacePolicy,
)
from local_first_agent_os.directives import DirectiveParser
from local_first_agent_os.graph_extraction import GraphExtractor, normalize_entity_name
from local_first_agent_os.ids import (
    build_graph_edge_id,
    build_graph_mention_id,
    build_graph_node_id,
)
from local_first_agent_os.model_manager import ModelNotLoadedError
from local_first_agent_os.observability import (
    GRAPH_ANALYTICS_LATENCY_SECONDS,
    GRAPH_AUGMENTED_QUERY_LATENCY_SECONDS,
    GRAPH_ENTITIES_EXTRACTED,
    GRAPH_EXTRACTION_LATENCY_SECONDS,
    GRAPH_NODES_CREATED_TOTAL,
    GRAPH_NODES_MERGED_TOTAL,
    GRAPH_RELATIONS_EXTRACTED,
    GRAPH_RESOLUTION_COLLISIONS_TOTAL,
)
from local_first_agent_os.runtime import AppRuntime
from local_first_agent_os.workflow.engine import WorkflowEngine
from local_first_agent_os.workflow.graph import GraphBatchTooLargeError

scenarios(
    "features/graph_extraction.feature",
    "features/graph_failure_modes.feature",
    "features/graph_resolution.feature",
    "features/graph_retrieval.feature",
    "features/graph_analytics_and_rebuild.feature",
    "features/graph_directive.feature",
    "features/graph_observability.feature",
)


GRAPH_METRICS: dict[str, tuple[MetricWrapperBase, str]] = {
    "graph_extraction_latency_seconds": (GRAPH_EXTRACTION_LATENCY_SECONDS, "histogram"),
    "graph_analytics_latency_seconds": (GRAPH_ANALYTICS_LATENCY_SECONDS, "histogram"),
    "graph_augmented_query_latency_seconds": (
        GRAPH_AUGMENTED_QUERY_LATENCY_SECONDS,
        "histogram",
    ),
    "graph_entities_extracted": (GRAPH_ENTITIES_EXTRACTED, "histogram"),
    "graph_relations_extracted": (GRAPH_RELATIONS_EXTRACTED, "histogram"),
    "graph_nodes_created_total": (GRAPH_NODES_CREATED_TOTAL, "counter"),
    "graph_nodes_merged_total": (GRAPH_NODES_MERGED_TOTAL, "counter"),
    "graph_resolution_collisions_total": (GRAPH_RESOLUTION_COLLISIONS_TOTAL, "counter"),
}


def _sample_total(metric: MetricWrapperBase, suffix: str) -> float:
    return sum(
        float(sample.value)
        for family in metric.collect()
        for sample in family.samples
        if sample.name.endswith(suffix)
    )


def _metric_snapshot(metric: MetricWrapperBase, kind: str) -> tuple[float, float]:
    if kind == "counter":
        return _sample_total(metric, "_total"), 0.0
    return _sample_total(metric, "_sum"), _sample_total(metric, "_count")


@dataclass(frozen=True)
class RunObservation:
    alias: str
    result: WorkflowResult
    calls_before: int
    calls_after: int

    @property
    def extractor_was_called(self) -> bool:
        return self.calls_after > self.calls_before


@dataclass
class GraphBDDContext:
    runtime: AppRuntime
    engine: WorkflowEngine
    caplog: pytest.LogCaptureFixture
    entity_types: dict[str, str] = field(default_factory=dict)
    relation_types: dict[str, str] = field(default_factory=dict)
    ontology_version: str = "v1"
    extraction_values: dict[str, Any] = field(
        default_factory=lambda: {
            "extractor_role": ModelRole.GENERAL,
            "resolution_threshold": 0.86,
            "review_threshold": 0.55,
            "max_extraction_chars": 24_000,
            "max_entities_per_artifact": 60,
            "max_batch_artifacts": 500,
        }
    )
    retrieval_values: dict[str, Any] = field(
        default_factory=lambda: {"max_hops": 2, "max_neighbors": 40}
    )
    artifacts: dict[str, ArtifactRef] = field(default_factory=dict)
    aliases_by_artifact_id: dict[str, str] = field(default_factory=dict)
    scripted_entities: list[dict[str, Any]] = field(default_factory=list)
    scripted_relations: list[dict[str, Any]] = field(default_factory=list)
    raw_output: str | None = None
    timeout_aliases: set[str] = field(default_factory=set)
    timeout_every_call: bool = False
    general_model_unloaded: bool = False
    embedder_unloaded: bool = False
    extractor_calls: int = 0
    vector_indices: dict[str, int] = field(default_factory=dict)
    explicit_vectors: dict[str, list[float]] = field(default_factory=dict)
    run_observations: list[RunObservation] = field(default_factory=list)
    batch_results: dict[str, WorkflowResult] = field(default_factory=dict)
    last_result: WorkflowResult | None = None
    last_error: Exception | None = None
    parsed_directive: DirectiveSpec | None = None
    parse_error: Exception | None = None
    directive_output: dict[str, Any] | None = None
    neighborhood: GraphNeighborhood | None = None
    vector_hits: dict[str, list[SearchHit]] = field(default_factory=dict)
    last_plain_hits: list[SearchHit] | None = None
    graph_snapshot: dict[str, Any] | None = None
    merge_failure_original: Any = None
    metric_baselines: dict[str, tuple[float, float]] = field(default_factory=dict)

    def config(self) -> GraphConfig:
        return GraphConfig(
            ontology=Ontology(
                ontology_version=self.ontology_version,
                entity_types=self.entity_types,
                relation_types=self.relation_types,
            ),
            extraction=GraphExtractionBounds.model_validate(self.extraction_values),
            retrieval=GraphRetrievalBounds.model_validate(self.retrieval_values),
        )

    def add_text_artifact(
        self,
        alias: str,
        text: str,
        *,
        role: str = ArtifactRole.NORMALIZED_TEXT.value,
        schema_version: str = "normalized_text.v1",
    ) -> ArtifactRef:
        artifact = self.runtime.artifact_store.write_text(
            role=role,
            text=text,
            workflow_id=None,
            schema_version=schema_version,
        )
        self.artifacts[alias] = artifact
        self.aliases_by_artifact_id[artifact.artifact_id] = alias
        return artifact

    def add_binary_artifact(self, alias: str, role: str) -> ArtifactRef:
        artifact = self.runtime.artifact_store.write_bytes(
            role=role,
            data=f"fixture:{alias}".encode(),
            workflow_id=None,
            mime_type="image/png",
            schema_version=f"{role}.v1",
        )
        self.artifacts[alias] = artifact
        self.aliases_by_artifact_id[artifact.artifact_id] = alias
        return artifact

    def vector_for(self, name: str) -> list[float]:
        normalized = normalize_entity_name(name)
        if normalized in self.explicit_vectors:
            return self.explicit_vectors[normalized]
        index = self.vector_indices.setdefault(normalized, len(self.vector_indices))
        vector = [0.0] * 64
        vector[index % len(vector)] = 1.0
        return vector

    def set_similarity(self, left: str, right: str, similarity: float) -> None:
        right_vector = self.vector_for(right)
        occupied = next(index for index, value in enumerate(right_vector) if value)
        free = (occupied + 1) % len(right_vector)
        left_vector = [0.0] * len(right_vector)
        left_vector[occupied] = similarity
        left_vector[free] = math.sqrt(max(0.0, 1.0 - similarity * similarity))
        self.explicit_vectors[normalize_entity_name(left)] = left_vector

    def ensure_artifact_for_chunk(self, chunk_id: str) -> ArtifactRef:
        alias = f"artifact-for-{chunk_id}"
        return self.artifacts.get(alias) or self.add_text_artifact(alias, f"text for {chunk_id}")

    def ensure_node(
        self,
        name: str,
        *,
        node_type: str | None = None,
        needs_review: bool = False,
    ) -> str:
        resolved_type = node_type or self.infer_node_type(name)
        normalized = normalize_entity_name(name)
        node_id = build_graph_node_id(resolved_type, normalized)
        self.runtime.repository.upsert_graph_node(
            node_id=node_id,
            node_type=resolved_type,
            canonical_name=name,
            normalized_name=normalized,
            artifact_id="bdd-fixture",
            embedding=self.vector_for(name),
            needs_review=needs_review,
            alias=name,
        )
        return node_id

    def infer_node_type(self, name: str) -> str:
        known = {
            "Rahul": "Person",
            "Rahul Nath": "Person",
            "R Nath": "Person",
            "Atlas": "Concept",
            "local agent OS": "Project",
            "DBOS": "Tool",
            "GAWD doc": "Concept",
            "the gawd document": "Concept",
            "the design doc": "Concept",
        }
        return known.get(name, "Concept")

    def find_nodes(self, name: str) -> list[Any]:
        normalized = normalize_entity_name(name)
        return [
            node
            for node in self.runtime.repository.list_graph_nodes()
            if node.normalized_name == normalized
            or name == node.canonical_name
            or name in node.aliases
        ]

    def one_node(self, name: str) -> Any:
        matches = self.find_nodes(name)
        assert matches, f"No graph node matched {name!r}"
        return matches[0]

    def one_edge(self, src_name: str, edge_type: str, dst_name: str) -> Any:
        src_ids = {node.node_id for node in self.find_nodes(src_name)}
        dst_ids = {node.node_id for node in self.find_nodes(dst_name)}
        matches = [
            edge
            for edge in self.runtime.repository.list_graph_edges()
            if edge.src_node_id in src_ids
            and edge.dst_node_id in dst_ids
            and edge.edge_type == edge_type
        ]
        assert len(matches) == 1, (
            f"Expected one {src_name} -{edge_type}-> {dst_name} edge, found {len(matches)}"
        )
        return matches[0]

    def add_edge(self, src_name: str, edge_type: str, dst_name: str) -> None:
        src_id = self.ensure_node(src_name)
        dst_id = self.ensure_node(dst_name)
        self.runtime.repository.upsert_graph_edge(
            edge_id=build_graph_edge_id(src_id, edge_type, dst_id),
            src_node_id=src_id,
            dst_node_id=dst_id,
            edge_type=edge_type,
            confidence=0.9,
            artifact_id="bdd-fixture",
        )

    def entity_graph_artifact(self, alias: str) -> tuple[ArtifactRef, EntityGraph] | None:
        source_id = self.artifacts[alias].artifact_id
        for ref in self.runtime.repository.list_artifacts_by_role(
            [ArtifactRole.ENTITY_GRAPH.value],
            limit=None,
        ):
            graph = EntityGraph.model_validate(
                self.runtime.artifact_store.read_json(ref.artifact_id)
            )
            if graph.source_artifact_id == source_id:
                return ref, graph
        return None

    def extraction_record(self) -> logging.LogRecord:
        records = [
            record
            for record in self.caplog.records
            if record.getMessage() == "graph_extraction_parsed"
        ]
        assert records
        return records[-1]

    def snapshot_graph(self) -> dict[str, Any]:
        return {
            "nodes": [
                node.model_dump(mode="json") for node in self.runtime.repository.list_graph_nodes()
            ],
            "edges": [
                edge.model_dump(mode="json") for edge in self.runtime.repository.list_graph_edges()
            ],
            "mentions": [
                mention.model_dump(mode="json")
                for mention in self.runtime.repository.list_graph_mentions()
            ],
        }


@pytest.fixture
def graph_context(
    runtime: AppRuntime,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> GraphBDDContext:
    caplog.set_level(logging.INFO)
    engine = WorkflowEngine(runtime)
    context = GraphBDDContext(runtime=runtime, engine=engine, caplog=caplog)
    context.metric_baselines = {
        name: _metric_snapshot(metric, kind) for name, (metric, kind) in GRAPH_METRICS.items()
    }

    monkeypatch.setattr(engine, "graph_config", context.config)

    def scripted_model_call(
        extractor: GraphExtractor,
        *,
        artifact: ArtifactRef,
        prompt: str,
        workflow_id: str,
    ) -> str:
        del extractor, prompt
        context.extractor_calls += 1
        alias = context.aliases_by_artifact_id.get(artifact.artifact_id, artifact.artifact_id)
        if context.timeout_every_call or alias in context.timeout_aliases:
            raise TimeoutError(f"scripted timeout for {alias}")
        if context.general_model_unloaded:
            raise ModelNotLoadedError(ModelRole.GENERAL)
        output = context.raw_output
        if output is None:
            output = json.dumps(
                {
                    "entities": context.scripted_entities,
                    "relations": context.scripted_relations,
                }
            )
        context.runtime.artifact_store.write_json(
            role=ArtifactRole.MODEL_OUTPUT.value,
            payload={
                "schema_version": "model_output.v1",
                "call": context.extractor_calls,
                "output": {"text": output},
            },
            workflow_id=workflow_id,
            schema_version="model_output.v1",
        )
        return output

    def require_loaded(role: ModelRole) -> None:
        if role is ModelRole.EMBEDDER and context.embedder_unloaded:
            raise ModelNotLoadedError(role)

    def embed_texts(texts: list[str], workflow_id: str) -> list[list[float]]:
        del workflow_id
        return [context.vector_for(text) for text in texts]

    def scripted_search(
        query: str,
        workspace_id: str | None = None,
        top_k: int = 50,
    ) -> list[SearchHit]:
        del workspace_id
        return list(context.vector_hits.get(query, []))[:top_k]

    monkeypatch.setattr(GraphExtractor, "_call_extractor", scripted_model_call)
    monkeypatch.setattr(runtime.model_manager, "require_loaded", require_loaded)
    monkeypatch.setattr(runtime.model_manager, "embed_texts", embed_texts)
    monkeypatch.setattr(runtime.retrieval, "search", scripted_search)
    return context


@given("the ontology declares the entity types:")
def declare_entity_types(graph_context: GraphBDDContext, datatable: list[list[str]]) -> None:
    graph_context.entity_types = {row[0]: row[1] for row in datatable[1:]}


@given("the ontology declares the relation types:")
def declare_relation_types(graph_context: GraphBDDContext, datatable: list[list[str]]) -> None:
    graph_context.relation_types = {row[0]: row[1] for row in datatable[1:]}


def _set_ontology_value(context: GraphBDDContext, key: str, value: str) -> None:
    target = (
        context.retrieval_values
        if key in GraphRetrievalBounds.model_fields
        else context.extraction_values
    )
    field_info = GraphRetrievalBounds.model_fields.get(
        key
    ) or GraphExtractionBounds.model_fields.get(key)
    assert field_info is not None, f"Unknown graph setting {key}"
    target[key] = float(value) if "." in value else int(value)


@given(parsers.parse('the ontology sets "{key}" to {value}'))
def set_ontology_value(graph_context: GraphBDDContext, key: str, value: str) -> None:
    _set_ontology_value(graph_context, key, value)


@when(parsers.parse('the ontology sets "{key}" to {value}'))
def change_ontology_value(graph_context: GraphBDDContext, key: str, value: str) -> None:
    _set_ontology_value(graph_context, key, value)


@given(parsers.parse('an embeddable artifact "{alias}" with the text:'))
def add_embeddable_artifact(
    graph_context: GraphBDDContext,
    alias: str,
    docstring: str,
) -> None:
    graph_context.add_text_artifact(alias, docstring)


@given(parsers.parse('a medical embeddable artifact "{alias}" with the text:'))
def add_medical_artifact(
    graph_context: GraphBDDContext,
    alias: str,
    docstring: str,
) -> None:
    graph_context.add_text_artifact(
        alias,
        docstring,
        schema_version="med_report.v1",
    )


@given(parsers.parse('a "{role}" artifact "{alias}"'))
def add_non_text_artifact(graph_context: GraphBDDContext, role: str, alias: str) -> None:
    graph_context.add_binary_artifact(alias, role)


@given(parsers.parse('an embeddable artifact "{alias}" with {size:d} characters of text'))
def add_sized_artifact(graph_context: GraphBDDContext, alias: str, size: int) -> None:
    graph_context.add_text_artifact(alias, "x" * size)


@given(parsers.parse('an embeddable artifact "{alias}" whose extraction times out'))
def add_timeout_artifact(graph_context: GraphBDDContext, alias: str) -> None:
    graph_context.add_text_artifact(alias, f"timeout fixture {alias}")
    graph_context.timeout_aliases.add(alias)


@given(parsers.parse("{count:d} embeddable artifacts"))
def add_embeddable_artifacts(graph_context: GraphBDDContext, count: int) -> None:
    for index in range(count):
        graph_context.add_text_artifact(f"note-{index}", f"fixture text {index}")


@given("the extractor returns the entities:")
def extractor_returns_entities(
    graph_context: GraphBDDContext,
    datatable: list[list[str]],
) -> None:
    headers = datatable[0]
    graph_context.scripted_entities = [
        {
            key: float(value) if key == "confidence" else value
            for key, value in zip(headers, row, strict=True)
        }
        for row in datatable[1:]
    ]


@when("the extractor returns the relations:")
@given("the extractor returns the relations:")
def extractor_returns_relations(
    graph_context: GraphBDDContext,
    datatable: list[list[str]],
) -> None:
    headers = datatable[0]
    graph_context.scripted_relations = [
        {
            key: float(value) if key == "confidence" else value
            for key, value in zip(headers, row, strict=True)
        }
        for row in datatable[1:]
    ]


@given(parsers.parse('the extractor returns {count:d} distinct "{node_type}" entities'))
def extractor_returns_many_entities(
    graph_context: GraphBDDContext,
    count: int,
    node_type: str,
) -> None:
    graph_context.scripted_entities = [
        {"name": f"entity-{index}", "node_type": node_type, "confidence": 0.9}
        for index in range(count)
    ]


@given("the extractor returns the raw output:")
def extractor_returns_raw_output(graph_context: GraphBDDContext, docstring: str) -> None:
    graph_context.raw_output = docstring


@given("the extractor times out")
def extractor_times_out(graph_context: GraphBDDContext) -> None:
    graph_context.timeout_every_call = True


@given("the general model is not loaded")
def general_model_not_loaded(graph_context: GraphBDDContext) -> None:
    graph_context.general_model_unloaded = True


@given("the embedder is not loaded")
def embedder_not_loaded(graph_context: GraphBDDContext) -> None:
    graph_context.embedder_unloaded = True


@given(parsers.parse('"{left}" has embedding similarity {similarity:g} to "{right}"'))
def set_embedding_similarity(
    graph_context: GraphBDDContext,
    left: str,
    similarity: float,
    right: str,
) -> None:
    graph_context.set_similarity(left, right, similarity)


@given(parsers.parse('the graph already contains a "{node_type}" node named "{name}"'))
def graph_already_contains_node(
    graph_context: GraphBDDContext,
    node_type: str,
    name: str,
) -> None:
    graph_context.ensure_node(name, node_type=node_type)


@given(
    parsers.parse(
        'the graph already contains a "{node_type}" node named "{name}" flagged for review'
    )
)
def graph_already_contains_review_node(
    graph_context: GraphBDDContext,
    node_type: str,
    name: str,
) -> None:
    graph_context.ensure_node(name, node_type=node_type, needs_review=True)


@given("the graph holds:")
def graph_holds_edges(graph_context: GraphBDDContext, datatable: list[list[str]]) -> None:
    headers = datatable[0]
    for row in datatable[1:]:
        edge = dict(zip(headers, row, strict=True))
        graph_context.add_edge(edge["src"], edge["edge_type"], edge["dst"])


@given("the graph is empty")
def graph_is_empty(graph_context: GraphBDDContext) -> None:
    graph_context.runtime.repository.drop_graph()


@given(parsers.parse('the node "{name}" has {count:d} neighbors'))
def node_has_neighbors(graph_context: GraphBDDContext, name: str, count: int) -> None:
    graph_context.ensure_node(name)
    for index in range(count):
        graph_context.add_edge(name, "RELATES_TO", f"neighbor-{index}")


@given(parsers.parse('the chunk "{chunk_id}" mentions the node "{name}"'))
def chunk_mentions_node(graph_context: GraphBDDContext, chunk_id: str, name: str) -> None:
    _add_chunk_mention(graph_context, chunk_id, name, "")


@given(
    parsers.parse('the chunk "{chunk_id}" mentions the node "{name}" with the snippet "{snippet}"')
)
def chunk_mentions_node_with_snippet(
    graph_context: GraphBDDContext,
    chunk_id: str,
    name: str,
    snippet: str,
) -> None:
    _add_chunk_mention(graph_context, chunk_id, name, snippet)


def _add_chunk_mention(
    context: GraphBDDContext,
    chunk_id: str,
    name: str,
    snippet: str,
) -> None:
    node_id = context.ensure_node(name)
    artifact = context.ensure_artifact_for_chunk(chunk_id)
    context.runtime.repository.upsert_graph_mention(
        mention_id=build_graph_mention_id(node_id, artifact.artifact_id, chunk_id),
        node_id=node_id,
        artifact_id=artifact.artifact_id,
        chunk_id=chunk_id,
        snippet=snippet,
    )


@given(parsers.parse('the chunk "{chunk_id}" mentions no node'))
def chunk_mentions_no_node(graph_context: GraphBDDContext, chunk_id: str) -> None:
    graph_context.ensure_artifact_for_chunk(chunk_id)


@given(parsers.parse('a vector search for "{query}" returns "{chunk_id}"'))
def vector_search_returns(
    graph_context: GraphBDDContext,
    query: str,
    chunk_id: str,
) -> None:
    artifact = graph_context.ensure_artifact_for_chunk(chunk_id)
    graph_context.vector_hits[query] = [
        SearchHit(
            chunk_id=chunk_id,
            artifact_id=artifact.artifact_id,
            workspace_id=WorkspaceId.GENERAL.value,
            text=f"vector hit {chunk_id}",
            score=0.9,
            metadata={},
        )
    ]


@given("the graph store fails after writing the first node")
def graph_store_fails_after_first_node(
    graph_context: GraphBDDContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = graph_context.runtime.repository.upsert_graph_node
    graph_context.merge_failure_original = original
    calls = 0

    def fail_second_node(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("scripted graph store failure")
        return original(**kwargs)

    monkeypatch.setattr(graph_context.runtime.repository, "upsert_graph_node", fail_second_node)


@when("the graph store recovers")
def graph_store_recovers(
    graph_context: GraphBDDContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert graph_context.merge_failure_original is not None
    monkeypatch.setattr(
        graph_context.runtime.repository,
        "upsert_graph_node",
        graph_context.merge_failure_original,
    )


@given("the medical workspace does not allow embedding its outputs")
def medical_embedding_is_disabled(graph_context: GraphBDDContext) -> None:
    policy = graph_context.runtime.policy_store.get(WorkspaceId.MEDICAL.value)
    assert not policy.embed_medical_outputs


@given(parsers.parse('the workspace policy root is "{root}"'))
def set_workspace_policy_root(graph_context: GraphBDDContext, root: str) -> None:
    graph_context.runtime.policy_store._policies = {
        WorkspaceId.GENERAL.value: WorkspacePolicy(
            workspace_id=WorkspaceId.GENERAL.value,
            root_path=Path(root),
        )
    }


def _extract_alias(context: GraphBDDContext, alias: str) -> WorkflowResult:
    artifact = context.artifacts[alias]
    calls_before = context.extractor_calls
    result = context.engine.extract_artifact_into_graph(artifact)
    context.last_result = result
    context.run_observations.append(
        RunObservation(
            alias=alias,
            result=result,
            calls_before=calls_before,
            calls_after=context.extractor_calls,
        )
    )
    return result


@when(parsers.parse('I extract the graph from "{alias}"'))
def extract_graph(graph_context: GraphBDDContext, alias: str) -> None:
    _extract_alias(graph_context, alias)


@when(parsers.parse('I extract the graph from "{alias}" again'))
def extract_graph_again(graph_context: GraphBDDContext, alias: str) -> None:
    _extract_alias(graph_context, alias)


@when("I extract the graph from the whole batch")
def extract_whole_batch(graph_context: GraphBDDContext) -> None:
    try:
        results = graph_context.engine.build_graph_over_artifacts(config=graph_context.config())
    except GraphBatchTooLargeError as exc:
        graph_context.last_error = exc
        return
    graph_context.batch_results = {
        observation.alias: observation.result for observation in graph_context.run_observations
    }
    if not graph_context.batch_results:
        for result in results:
            source_graphs = [
                graph
                for alias in graph_context.artifacts
                if (found := graph_context.entity_graph_artifact(alias)) is not None
                and found[0] in result.artifacts
                for graph in [alias]
            ]
            if source_graphs:
                graph_context.batch_results[source_graphs[0]] = result
        for alias, result in zip(graph_context.artifacts, results, strict=False):
            graph_context.batch_results.setdefault(alias, result)
    if results:
        graph_context.last_result = results[-1]


@when("I run graph analytics")
def run_graph_analytics(graph_context: GraphBDDContext) -> None:
    graph_context.last_result = graph_context.engine.graph_analytics(
        graph_context.engine._graph_event("bdd_graph_analytics")
    )


@given("graph analytics has run")
def graph_analytics_has_run(graph_context: GraphBDDContext) -> None:
    run_graph_analytics(graph_context)


@when(parsers.parse('I ask the graph "{query}"'))
def ask_graph(graph_context: GraphBDDContext, query: str) -> None:
    _, graph_context.neighborhood = graph_context.runtime.retrieval.graph_augmented_context(
        query,
        retrieval_bounds=graph_context.config().retrieval,
    )


@when(parsers.parse('I run plain retrieval for "{query}"'))
def run_plain_retrieval(graph_context: GraphBDDContext, query: str) -> None:
    graph_context.last_plain_hits = graph_context.runtime.retrieval.search(query)


@when("I snapshot the graph")
def snapshot_graph(graph_context: GraphBDDContext) -> None:
    graph_context.graph_snapshot = graph_context.snapshot_graph()


@when("I rebuild the graph")
def rebuild_graph(graph_context: GraphBDDContext) -> None:
    graph_context.engine.rebuild_graph(config=graph_context.config())


@when(parsers.parse('I parse the directive "{directive}"'))
def parse_directive(graph_context: GraphBDDContext, directive: str) -> None:
    parser = DirectiveParser(graph_context.runtime.settings)
    try:
        graph_context.parsed_directive = parser.parse(directive)
    except ValueError as exc:
        graph_context.parse_error = exc


def _directive_event(directive: str) -> IngressEvent:
    return IngressEvent(
        event_id=f"bdd-directive:{directive}",
        source_type=SourceType.MANUAL,
        event_type="model_directive",
        workspace_id=WorkspaceId.GENERAL.value,
        source_uri=f"bdd://directive/{directive}",
        payload={"directive": directive},
    )


@when(parsers.parse('I run "{directive}"'))
def run_directive(graph_context: GraphBDDContext, directive: str) -> None:
    graph_context.last_result = graph_context.engine.model_directive(_directive_event(directive))
    assert graph_context.last_result.artifacts
    output = graph_context.runtime.artifact_store.read_json(
        graph_context.last_result.artifacts[-1].artifact_id
    )
    assert isinstance(output, dict)
    graph_context.directive_output = output
    if output.get("error"):
        graph_context.last_error = RuntimeError(str(output["error"]))


@when(parsers.parse('the ontology version becomes "{version}"'))
def ontology_version_becomes(graph_context: GraphBDDContext, version: str) -> None:
    graph_context.ontology_version = version


@when(parsers.parse('the graph gains an orphan node "{name}" that no artifact asserts'))
@given(parsers.parse('the graph gains an orphan node "{name}" that no artifact asserts'))
def add_orphan_node(graph_context: GraphBDDContext, name: str) -> None:
    graph_context.ensure_node(name)


@then(parsers.parse('the workflow completes with status "{status}"'))
def workflow_has_status(graph_context: GraphBDDContext, status: str) -> None:
    assert graph_context.last_result is not None
    assert graph_context.last_result.status is WorkflowStatus(status)


@then(parsers.parse('the workflow stage is "{stage}"'))
def workflow_has_stage(graph_context: GraphBDDContext, stage: str) -> None:
    assert graph_context.last_result is not None
    assert graph_context.last_result.current_stage.value == stage


@then("the workflow passed through the stages:")
def workflow_passed_stages(
    graph_context: GraphBDDContext,
    datatable: list[list[str]],
) -> None:
    assert graph_context.last_result is not None
    expected = [row[0] for row in datatable[1:]]
    actual = [
        stage.value
        for stage in graph_context.runtime.repository.list_workflow_stage_transitions(
            graph_context.last_result.workflow_id
        )
        if stage.value != "REGISTERED"
    ]
    assert actual == expected


@then(parsers.parse('an "{role}" artifact is persisted for "{alias}"'))
@then(parsers.parse('a "{role}" artifact is persisted for "{alias}"'))
def artifact_is_persisted_for(
    graph_context: GraphBDDContext,
    role: str,
    alias: str,
) -> None:
    if role == ArtifactRole.ENTITY_GRAPH.value:
        assert graph_context.entity_graph_artifact(alias) is not None
        return
    assert graph_context.last_result is not None
    assert any(str(artifact.role) == role for artifact in graph_context.last_result.artifacts)


@then(parsers.parse('a "{role}" artifact is persisted'))
def artifact_is_persisted(graph_context: GraphBDDContext, role: str) -> None:
    assert graph_context.last_result is not None
    assert any(str(artifact.role) == role for artifact in graph_context.last_result.artifacts)


@then(parsers.parse('no "{role}" artifact is persisted'))
def artifact_is_not_persisted(graph_context: GraphBDDContext, role: str) -> None:
    assert not graph_context.runtime.repository.list_artifacts_by_role([role], limit=None)


@then(parsers.parse('the graph contains a "{node_type}" node named "{name}"'))
def graph_contains_node(
    graph_context: GraphBDDContext,
    node_type: str,
    name: str,
) -> None:
    assert any(node.node_type == node_type for node in graph_context.find_nodes(name))


@then(parsers.parse('the graph does not contain a node named "{name}"'))
def graph_does_not_contain_node(graph_context: GraphBDDContext, name: str) -> None:
    assert not graph_context.find_nodes(name)


@then(parsers.parse("the graph has {nodes:d} nodes and {edges:d} edges"))
def graph_has_counts(graph_context: GraphBDDContext, nodes: int, edges: int) -> None:
    assert len(graph_context.runtime.repository.list_graph_nodes()) == nodes
    assert len(graph_context.runtime.repository.list_graph_edges()) == edges


@then(parsers.parse('the node "{name}" has 1 mention of "{alias}"'))
def node_has_artifact_mention(
    graph_context: GraphBDDContext,
    name: str,
    alias: str,
) -> None:
    node = graph_context.one_node(name)
    artifact_id = graph_context.artifacts[alias].artifact_id
    mentions = [
        mention
        for mention in graph_context.runtime.repository.list_graph_mentions(node.node_id)
        if mention.artifact_id == artifact_id
    ]
    assert len(mentions) == 1


@then(parsers.parse('the node "{name}" has mention count {count:d}'))
def node_has_mention_count(
    graph_context: GraphBDDContext,
    name: str,
    count: int,
) -> None:
    assert graph_context.one_node(name).mention_count == count


@then(parsers.parse("{count:d} entity was dropped as an unknown type"))
def unknown_entity_count(graph_context: GraphBDDContext, count: int) -> None:
    record = cast(Any, graph_context.extraction_record())
    assert record.dropped_entity_types == count


@then(parsers.parse("{count:d} relation was dropped as an unknown type"))
def unknown_relation_count(graph_context: GraphBDDContext, count: int) -> None:
    record = cast(Any, graph_context.extraction_record())
    assert record.dropped_relation_types == count


@then(parsers.parse("{count:d} relation was dropped as dangling"))
def dangling_relation_count(graph_context: GraphBDDContext, count: int) -> None:
    record = cast(Any, graph_context.extraction_record())
    assert record.dropped_dangling_relations == count


@then("the extraction is flagged truncated")
def extraction_is_truncated(graph_context: GraphBDDContext) -> None:
    record = cast(Any, graph_context.extraction_record())
    assert record.entity_count == int(graph_context.extraction_values["max_entities_per_artifact"])


@then(parsers.parse('the "{role}" artifact for "{alias}" is flagged truncated'))
def entity_graph_artifact_is_truncated(
    graph_context: GraphBDDContext,
    role: str,
    alias: str,
) -> None:
    assert role == ArtifactRole.ENTITY_GRAPH.value
    found = graph_context.entity_graph_artifact(alias)
    assert found is not None and found[1].truncated


@then(parsers.parse('the "{role}" artifact for "{alias}" records the ontology version'))
def artifact_records_ontology(
    graph_context: GraphBDDContext,
    role: str,
    alias: str,
) -> None:
    assert role == ArtifactRole.ENTITY_GRAPH.value
    found = graph_context.entity_graph_artifact(alias)
    assert found is not None
    assert found[1].ontology_version == graph_context.ontology_version


@then(parsers.parse('the "{role}" artifact for "{alias}" records the extractor model id'))
def artifact_records_extractor(
    graph_context: GraphBDDContext,
    role: str,
    alias: str,
) -> None:
    assert role == ArtifactRole.ENTITY_GRAPH.value
    found = graph_context.entity_graph_artifact(alias)
    assert found is not None and found[1].extractor_model_id


@then(parsers.parse('the "{role}" artifact for "{alias}" names "{source}" as its source'))
def artifact_records_source(
    graph_context: GraphBDDContext,
    role: str,
    alias: str,
    source: str,
) -> None:
    assert role == ArtifactRole.ENTITY_GRAPH.value
    found = graph_context.entity_graph_artifact(alias)
    assert found is not None
    assert found[1].source_artifact_id == graph_context.artifacts[source].artifact_id


@then("the batch is rejected with an error naming the limit")
def batch_rejected_at_limit(graph_context: GraphBDDContext) -> None:
    assert isinstance(graph_context.last_error, GraphBatchTooLargeError)
    assert "max_batch_artifacts" in str(graph_context.last_error)


@then(parsers.parse("the extractor was called {count:d} times"))
def extractor_call_count(graph_context: GraphBDDContext, count: int) -> None:
    assert graph_context.extractor_calls == count


@then(parsers.parse('the node "{name}" is flagged for review'))
def node_is_flagged(graph_context: GraphBDDContext, name: str) -> None:
    assert graph_context.one_node(name).needs_review


@then(parsers.parse('the node "{name}" is not flagged for review'))
def node_is_not_flagged(graph_context: GraphBDDContext, name: str) -> None:
    assert not graph_context.one_node(name).needs_review


@then(parsers.parse('the edge "{src}" -"{edge_type}"-> "{dst}" is flagged for review'))
def edge_is_flagged(
    graph_context: GraphBDDContext,
    src: str,
    edge_type: str,
    dst: str,
) -> None:
    assert graph_context.one_edge(src, edge_type, dst).needs_review


@then(parsers.parse('the run for "{alias}" completes with status "{status}"'))
def batch_run_has_status(
    graph_context: GraphBDDContext,
    alias: str,
    status: str,
) -> None:
    assert graph_context.batch_results[alias].status is WorkflowStatus(status)


@then("the run is flagged as resolution-degraded")
def run_resolution_degraded(graph_context: GraphBDDContext) -> None:
    assert graph_context.last_result is not None
    assert graph_context.last_result.embedding_degraded


@then(parsers.parse('the node "{name}" was merged by the "{path}" path'))
def node_was_merged_by(
    graph_context: GraphBDDContext,
    name: str,
    path: str,
) -> None:
    node_id = graph_context.one_node(name).node_id
    assert any(
        record.getMessage() == "graph_node_written"
        and getattr(record, "node_id", None) == node_id
        and getattr(record, "outcome", None) == "merged"
        and getattr(record, "resolution_path", None) == path
        for record in graph_context.caplog.records
    )


@then(parsers.parse('the node "{name}" was created'))
def node_was_created(graph_context: GraphBDDContext, name: str) -> None:
    node_id = graph_context.one_node(name).node_id
    assert any(
        record.getMessage() == "graph_node_written"
        and getattr(record, "node_id", None) == node_id
        and getattr(record, "outcome", None) == "created"
        for record in graph_context.caplog.records
    )


@then(parsers.parse('the node "{name}" has aliases "{alias}"'))
def node_has_alias(
    graph_context: GraphBDDContext,
    name: str,
    alias: str,
) -> None:
    assert alias in graph_context.one_node(name).aliases


@then("the second run was skipped as already completed")
def second_run_was_skipped(graph_context: GraphBDDContext) -> None:
    assert len(graph_context.run_observations) >= 2
    assert not graph_context.run_observations[-1].extractor_was_called


@then("the second run was not skipped")
def second_run_was_not_skipped(graph_context: GraphBDDContext) -> None:
    assert len(graph_context.run_observations) >= 2
    assert graph_context.run_observations[-1].extractor_was_called


@then(parsers.parse('the edge "{src}" -"{edge_type}"-> "{dst}" has weight {weight:d}'))
def edge_has_weight(
    graph_context: GraphBDDContext,
    src: str,
    edge_type: str,
    dst: str,
    weight: int,
) -> None:
    assert graph_context.one_edge(src, edge_type, dst).weight == weight


@then(parsers.parse('the edge "{src}" -"{edge_type}"-> "{dst}" has confidence {confidence:g}'))
def edge_has_confidence(
    graph_context: GraphBDDContext,
    src: str,
    edge_type: str,
    dst: str,
    confidence: float,
) -> None:
    assert graph_context.one_edge(src, edge_type, dst).confidence == pytest.approx(confidence)


@then(parsers.parse('the edge "{src}" -"{edge_type}"-> "{dst}" cites both artifacts'))
def edge_cites_both_artifacts(
    graph_context: GraphBDDContext,
    src: str,
    edge_type: str,
    dst: str,
) -> None:
    edge = graph_context.one_edge(src, edge_type, dst)
    expected = {
        graph_context.artifacts["note-alpha"].artifact_id,
        graph_context.artifacts["note-beta"].artifact_id,
    }
    assert set(edge.source_artifact_ids) == expected


@then('the neighborhood seeds are ""')
@then(parsers.parse('the neighborhood seeds are "{names}"'))
def neighborhood_has_seeds(graph_context: GraphBDDContext, names: str = "") -> None:
    assert graph_context.neighborhood is not None
    expected_ids = (
        {graph_context.one_node(name.strip()).node_id for name in names.split(",") if name.strip()}
        if names
        else set()
    )
    assert set(graph_context.neighborhood.seed_node_ids) == expected_ids


@then(parsers.parse('the neighborhood contains the node "{name}"'))
def neighborhood_contains_node(graph_context: GraphBDDContext, name: str) -> None:
    assert graph_context.neighborhood is not None
    assert any(
        node["node_id"] == graph_context.one_node(name).node_id
        for node in graph_context.neighborhood.nodes
    )


@then(parsers.parse('the neighborhood contains the node "{name}" at {hops:d} hops'))
def neighborhood_contains_node_at_hops(
    graph_context: GraphBDDContext,
    name: str,
    hops: int,
) -> None:
    assert graph_context.neighborhood is not None
    node_id = graph_context.one_node(name).node_id
    assert any(
        node["node_id"] == node_id and node["hops"] == hops
        for node in graph_context.neighborhood.nodes
    )


@then(parsers.parse('the neighborhood does not contain the node "{name}"'))
def neighborhood_does_not_contain_node(
    graph_context: GraphBDDContext,
    name: str,
) -> None:
    assert graph_context.neighborhood is not None
    node_ids = {node.node_id for node in graph_context.find_nodes(name)}
    assert all(node["node_id"] not in node_ids for node in graph_context.neighborhood.nodes)


@then(parsers.parse('the neighborhood contains the edge "{src}" -"{edge_type}"-> "{dst}"'))
def neighborhood_contains_edge(
    graph_context: GraphBDDContext,
    src: str,
    edge_type: str,
    dst: str,
) -> None:
    assert graph_context.neighborhood is not None
    edge_id = graph_context.one_edge(src, edge_type, dst).edge_id
    assert any(edge["edge_id"] == edge_id for edge in graph_context.neighborhood.edges)


@then(parsers.parse("the neighborhood contains {count:d} nodes besides the seeds"))
def neighborhood_has_nonseed_count(graph_context: GraphBDDContext, count: int) -> None:
    assert graph_context.neighborhood is not None
    seeds = set(graph_context.neighborhood.seed_node_ids)
    assert sum(node["node_id"] not in seeds for node in graph_context.neighborhood.nodes) == count


@then(parsers.parse('the neighborhood carries the snippet "{snippet}" for "{name}"'))
def neighborhood_has_snippet(
    graph_context: GraphBDDContext,
    snippet: str,
    name: str,
) -> None:
    assert graph_context.neighborhood is not None
    node_id = graph_context.one_node(name).node_id
    assert any(
        mention["node_id"] == node_id and mention["snippet"] == snippet
        for mention in graph_context.neighborhood.mention_snippets
    )


@then("the result is exactly the vector hits")
def plain_result_is_vector_hits(graph_context: GraphBDDContext) -> None:
    assert graph_context.last_plain_hits == graph_context.vector_hits["who writes the notes"]


@then("every node has a pagerank score")
def every_node_has_pagerank(graph_context: GraphBDDContext) -> None:
    assert all(
        node.pagerank is not None for node in graph_context.runtime.repository.list_graph_nodes()
    )


@then("every node has a degree")
def every_node_has_degree(graph_context: GraphBDDContext) -> None:
    assert all(
        node.degree is not None for node in graph_context.runtime.repository.list_graph_nodes()
    )


@then("every node has a community id")
def every_node_has_community(graph_context: GraphBDDContext) -> None:
    assert all(
        node.community_id is not None
        for node in graph_context.runtime.repository.list_graph_nodes()
    )


@then(parsers.parse('the node "{name}" has degree {degree:d}'))
def node_has_degree(
    graph_context: GraphBDDContext,
    name: str,
    degree: int,
) -> None:
    assert graph_context.one_node(name).degree == degree


@then(parsers.parse('the nodes "{left}" and "{right}" share a community'))
def nodes_share_community(
    graph_context: GraphBDDContext,
    left: str,
    right: str,
) -> None:
    assert graph_context.one_node(left).community_id == graph_context.one_node(right).community_id


@then(parsers.parse('the nodes "{left}" and "{right}" do not share a community'))
def nodes_do_not_share_community(
    graph_context: GraphBDDContext,
    left: str,
    right: str,
) -> None:
    assert graph_context.one_node(left).community_id != graph_context.one_node(right).community_id


@then("the graph matches the snapshot")
def graph_matches_snapshot(graph_context: GraphBDDContext) -> None:
    assert graph_context.graph_snapshot is not None
    assert graph_context.snapshot_graph() == graph_context.graph_snapshot


@then(parsers.parse('the directive action is "{action}"'))
def directive_has_action(graph_context: GraphBDDContext, action: str) -> None:
    assert graph_context.parsed_directive is not None
    assert graph_context.parsed_directive.action == action


@then(parsers.parse('the graph subcommand is "{subcommand}"'))
def directive_has_subcommand(graph_context: GraphBDDContext, subcommand: str) -> None:
    assert graph_context.parsed_directive is not None
    assert graph_context.parsed_directive.graph_subcommand is not None
    assert graph_context.parsed_directive.graph_subcommand.value == subcommand


@then("parsing fails with an error naming the subcommands")
def parse_error_names_subcommands(graph_context: GraphBDDContext) -> None:
    assert graph_context.parse_error is not None
    message = str(graph_context.parse_error)
    assert "build" in message and "rebuild" in message


@then("parsing fails with an error naming the missing argument")
def parse_error_names_missing_argument(graph_context: GraphBDDContext) -> None:
    assert graph_context.parse_error is not None
    assert "requires a quoted argument" in str(graph_context.parse_error)


@then(parsers.parse('the directive path is "{path}"'))
def directive_has_path(graph_context: GraphBDDContext, path: str) -> None:
    assert graph_context.parsed_directive is not None
    assert graph_context.parsed_directive.path == Path(path)


@then("the directive has no path")
def directive_has_no_path(graph_context: GraphBDDContext) -> None:
    assert graph_context.parsed_directive is not None
    assert graph_context.parsed_directive.path is None


@then("the run is refused with a path-policy error")
def run_refused_by_path_policy(graph_context: GraphBDDContext) -> None:
    assert graph_context.last_error is not None
    assert "outside every workspace policy root" in str(graph_context.last_error)


@then(parsers.parse('the review output lists "{name}"'))
def review_output_lists(graph_context: GraphBDDContext, name: str) -> None:
    assert graph_context.directive_output is not None
    assert any(node["canonical_name"] == name for node in graph_context.directive_output["nodes"])


@then(parsers.parse('the review output does not list "{name}"'))
def review_output_excludes(graph_context: GraphBDDContext, name: str) -> None:
    assert graph_context.directive_output is not None
    assert all(node["canonical_name"] != name for node in graph_context.directive_output["nodes"])


@then(parsers.parse("the stats report {nodes:d} nodes and {edges:d} edges"))
def stats_report_counts(
    graph_context: GraphBDDContext,
    nodes: int,
    edges: int,
) -> None:
    assert graph_context.directive_output is not None
    assert graph_context.directive_output["node_count"] == nodes
    assert graph_context.directive_output["edge_count"] == edges


@then("the stats name the top-ranked node")
def stats_have_top_node(graph_context: GraphBDDContext) -> None:
    assert graph_context.directive_output is not None
    assert graph_context.directive_output["top_nodes"]


@then(parsers.parse('the metric "{name}" was observed'))
def metric_was_observed(graph_context: GraphBDDContext, name: str) -> None:
    metric, kind = GRAPH_METRICS[name]
    _, baseline_count = graph_context.metric_baselines[name]
    _, current_count = _metric_snapshot(metric, kind)
    assert current_count - baseline_count >= 1


@then(parsers.parse('the metric "{name}" recorded {expected:g}'))
def metric_recorded(
    graph_context: GraphBDDContext,
    name: str,
    expected: float,
) -> None:
    metric, kind = GRAPH_METRICS[name]
    baseline_value, _ = graph_context.metric_baselines[name]
    current_value, _ = _metric_snapshot(metric, kind)
    assert current_value - baseline_value == pytest.approx(expected)


@then(parsers.parse('every metric observed in this run is labelled "{workflow_type}"'))
def observed_metrics_have_workflow_type(
    graph_context: GraphBDDContext,
    workflow_type: str,
) -> None:
    changed_metrics: list[MetricWrapperBase] = []
    for name, (metric, kind) in GRAPH_METRICS.items():
        baseline_value = graph_context.metric_baselines[name][0]
        current_value = _metric_snapshot(metric, kind)[0]
        if current_value > baseline_value:
            changed_metrics.append(metric)

    assert changed_metrics
    assert all(
        any(
            sample.labels.get("workflow_type") == workflow_type
            for family in metric.collect()
            for sample in family.samples
        )
        for metric in changed_metrics
    )


@then(parsers.parse('no INFO log line contains "{text}"'))
def info_logs_exclude_text(graph_context: GraphBDDContext, text: str) -> None:
    assert all(
        text not in record.getMessage()
        for record in graph_context.caplog.records
        if record.levelno == logging.INFO
    )


def _graph_log_records(context: GraphBDDContext) -> list[logging.LogRecord]:
    return [
        record
        for record in context.caplog.records
        if record.getMessage().startswith("graph_") and record.levelno >= logging.INFO
    ]


@then("every graph log line carries the workflow id")
def graph_logs_have_workflow_id(graph_context: GraphBDDContext) -> None:
    records = _graph_log_records(graph_context)
    assert records
    assert all(getattr(record, "workflow_id", None) for record in records)


@then("every graph log line carries the artifact id")
def graph_logs_have_artifact_id(graph_context: GraphBDDContext) -> None:
    records = _graph_log_records(graph_context)
    assert records
    assert all(getattr(record, "artifact_id", None) for record in records)
