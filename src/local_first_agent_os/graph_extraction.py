# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Turning one artifact's text into entity-graph rows.

The pipeline is deliberately split into three phases with different failure
characters:

1. *Extraction* asks the `general` model for entities and relations. This is
   the only non-deterministic step, and the only one that can fail permanently.
2. *Ontology filtering* is pure: it drops what the closed ontology does not
   name, drops relations whose endpoints were never extracted, and caps the
   result. Nothing here can fail, only discard.
3. *Merge* resolves each surviving entity onto the graph and upserts. It is
   idempotent by construction, so a merge that dies partway is safe to re-run.

Only phase 1 needs a model. Phases 2 and 3 are what the Gherkin scenarios
mostly pin down, because they are where the doc's invariants live.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .artifacts import ArtifactStore
from .constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
from .contracts import (
    ArtifactRef,
    EntityGraph,
    ExtractedEntity,
    ExtractedRelation,
    GraphConfig,
    GraphWriteOutcome,
    ModelCallRequest,
    ModelRole,
    NodeResolutionPath,
    Ontology,
    WorkflowType,
)
from .ids import build_graph_edge_id, build_graph_mention_id, build_graph_node_id
from .model_manager import ModelManager, ModelNotLoadedError
from .observability import (
    GRAPH_ENTITIES_EXTRACTED,
    GRAPH_NODES_CREATED_TOTAL,
    GRAPH_NODES_MERGED_TOTAL,
    GRAPH_RELATIONS_EXTRACTED,
    GRAPH_RESOLUTION_COLLISIONS_TOTAL,
)
from .repository import Repository

logger = logging.getLogger(__name__)

_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
_NON_ALPHANUMERIC = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


class MalformedExtractionError(ValueError):
    """The model's output was not a usable entity graph, even after repair."""


def normalize_entity_name(name: str) -> str:
    """Fold surface variation that never distinguishes two entities.

    Case, surrounding punctuation, a leading article, and runs of whitespace
    are all noise: "The GAWD Doc." and "GAWD doc" are the same thing. Anything
    beyond that (abbreviation, nicknames) is left to embedding resolution,
    which is threshold-gated and therefore reversible.
    """
    folded = _NON_ALPHANUMERIC.sub(" ", name.strip().lower())
    folded = _WHITESPACE.sub(" ", folded).strip()
    return _LEADING_ARTICLE.sub("", folded).strip()


def window_text(text: str, max_chars: int) -> list[str]:
    """Split text into extraction-sized windows.

    Each window is its own model call and the results are unioned, so a long
    note costs more calls rather than silently losing its tail.
    """
    if max_chars <= 0:
        raise ValueError("max_extraction_chars must be positive.")
    if not text:
        return [""]
    return [text[start : start + max_chars] for start in range(0, len(text), max_chars)]


def build_extraction_prompt(ontology: Ontology, window: str) -> str:
    """Constrain the model to the closed ontology, in the prompt and after it.

    Naming the legal types inline is a cost reduction, not a guarantee: the
    ontology filter drops anything else regardless of what the model returns.
    """
    entity_types = "\n".join(
        f"- {name}: {description}" for name, description in sorted(ontology.entity_types.items())
    )
    relation_types = "\n".join(
        f"- {name}: {description}" for name, description in sorted(ontology.relation_types.items())
    )
    return (
        "Extract entities and relationships from the text below.\n\n"
        "Use ONLY these entity types:\n"
        f"{entity_types}\n\n"
        "Use ONLY these relationship types:\n"
        f"{relation_types}\n\n"
        "Return a single JSON object and nothing else, shaped exactly like:\n"
        '{"entities": [{"name": str, "node_type": str, "description": str, '
        '"confidence": float, "snippet": str}], '
        '"relations": [{"src_name": str, "dst_name": str, "edge_type": str, '
        '"confidence": float, "snippet": str}]}\n\n'
        "Every relation's src_name and dst_name must be the name of an entity you "
        "returned. Confidence is your own 0-1 estimate; be honest rather than "
        "generous, because low-confidence rows are quarantined for review.\n\n"
        "TEXT:\n"
        f"{window}"
    )


def decode_extraction_json(raw_text: str) -> dict[str, Any]:
    """Read a JSON object out of model text that may be wrapped in prose.

    Fences and a chatty preamble are ordinary model behavior, not a failure,
    so they are handled here rather than costing a repair round-trip.
    """
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    if not stripped.startswith("{"):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise MalformedExtractionError("Extractor output contained no JSON object.")
        stripped = stripped[start : end + 1]
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise MalformedExtractionError(f"Extractor output was not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise MalformedExtractionError("Extractor output was not a JSON object.")
    return loaded


def parse_extraction_payload(
    payload: dict[str, Any],
) -> tuple[list[ExtractedEntity], list[ExtractedRelation]]:
    """Validate the decoded object into contracts, skipping unusable rows.

    A single malformed row is discarded rather than failing the artifact: the
    doc's permanent-failure case is output that yields no graph at all.
    """
    if "entities" not in payload or "relations" not in payload:
        raise MalformedExtractionError(
            "Extractor output must contain entities and relations arrays."
        )
    raw_entities = payload["entities"]
    raw_relations = payload["relations"]
    if not isinstance(raw_entities, list) or not isinstance(raw_relations, list):
        raise MalformedExtractionError("Extractor entities and relations must both be arrays.")

    entities: list[ExtractedEntity] = []
    invalid_rows = 0
    for item in raw_entities:
        if not isinstance(item, dict):
            invalid_rows += 1
            continue
        try:
            entities.append(ExtractedEntity.model_validate(item))
        except ValueError:
            invalid_rows += 1
            logger.debug("graph_extraction_entity_discarded")
    relations: list[ExtractedRelation] = []
    for item in raw_relations:
        if not isinstance(item, dict):
            invalid_rows += 1
            continue
        try:
            relations.append(ExtractedRelation.model_validate(item))
        except ValueError:
            invalid_rows += 1
            logger.debug("graph_extraction_relation_discarded")
    if invalid_rows and not entities and not relations:
        raise MalformedExtractionError(
            "Extractor output contained rows, but none satisfied the entity graph contract."
        )
    return entities, relations


@dataclass(frozen=True)
class OntologyFilterResult:
    """What survived the closed ontology, and what did not.

    The drop counts are part of the result rather than a log line because §8
    treats them as an observable: a spike in dropped types is how a drifting
    extractor becomes visible.
    """

    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)
    dropped_entity_types: int = 0
    dropped_relation_types: int = 0
    dropped_dangling_relations: int = 0
    truncated: bool = False


def apply_ontology(
    *,
    ontology: Ontology,
    max_entities: int,
    entities: list[ExtractedEntity],
    relations: list[ExtractedRelation],
) -> OntologyFilterResult:
    """Reduce a raw extraction to what the graph is allowed to contain.

    Order matters: types are checked before the cap, and the cap is applied
    before dangling relations are resolved, so truncating entities also
    truncates the relations that depended on them.
    """
    kept_entities: list[ExtractedEntity] = []
    seen_entities: set[tuple[str, str]] = set()
    dropped_entity_types = 0
    for entity in entities:
        if not ontology.allows_entity(entity.node_type):
            dropped_entity_types += 1
            continue
        normalized = normalize_entity_name(entity.name)
        identity = (entity.node_type, normalized)
        if not normalized or identity in seen_entities:
            continue
        seen_entities.add(identity)
        kept_entities.append(entity)

    truncated = len(kept_entities) > max_entities
    kept_entities = kept_entities[:max_entities]
    reachable = {normalize_entity_name(entity.name) for entity in kept_entities}

    kept_relations: list[ExtractedRelation] = []
    dropped_relation_types = 0
    dropped_dangling = 0
    for relation in relations:
        if not ontology.allows_relation(relation.edge_type):
            dropped_relation_types += 1
            continue
        src = normalize_entity_name(relation.src_name)
        dst = normalize_entity_name(relation.dst_name)
        if src not in reachable or dst not in reachable or src == dst:
            dropped_dangling += 1
            continue
        kept_relations.append(relation)

    return OntologyFilterResult(
        entities=kept_entities,
        relations=kept_relations,
        dropped_entity_types=dropped_entity_types,
        dropped_relation_types=dropped_relation_types,
        dropped_dangling_relations=dropped_dangling,
        truncated=truncated,
    )


@dataclass
class MergeOutcome:
    """Counters the §1.3 metrics and §10 runbook are written against."""

    nodes_created: int = 0
    nodes_merged: int = 0
    edges_created: int = 0
    edges_merged: int = 0
    mentions_created: int = 0
    resolution_collisions: int = 0
    resolution_degraded: bool = False


class GraphExtractor:
    """Runs the extraction model and merges its output into the graph."""

    def __init__(
        self,
        *,
        repository: Repository,
        artifact_store: ArtifactStore,
        model_manager: ModelManager,
        config: GraphConfig,
    ) -> None:
        self.repository = repository
        self.artifact_store = artifact_store
        self.model_manager = model_manager
        self.config = config

    @property
    def extractor_model_id(self) -> str:
        return self.model_manager.registry.resolve_model(
            self.config.extraction.extractor_role
        ).model_id

    def extract(self, *, artifact: ArtifactRef, text: str, workflow_id: str) -> EntityGraph:
        """Produce this artifact's `entity_graph.v1` payload.

        Raises MalformedExtractionError only when the model produced nothing
        usable across both the initial call and its one repair attempt; that is
        the doc's FAILED_PERMANENT boundary.
        """
        entities: list[ExtractedEntity] = []
        relations: list[ExtractedRelation] = []
        for window in window_text(text, self.config.extraction.max_extraction_chars):
            window_entities, window_relations = self._extract_window(
                artifact=artifact,
                window=window,
                workflow_id=workflow_id,
            )
            entities.extend(window_entities)
            relations.extend(window_relations)

        filtered = apply_ontology(
            ontology=self.config.ontology,
            max_entities=self.config.extraction.max_entities_per_artifact,
            entities=entities,
            relations=relations,
        )
        metric_labels = {"workflow_type": WorkflowType.GRAPH_EXTRACTION.value}
        GRAPH_ENTITIES_EXTRACTED.labels(**metric_labels).observe(len(filtered.entities))
        GRAPH_RELATIONS_EXTRACTED.labels(**metric_labels).observe(len(filtered.relations))
        logger.info(
            "graph_extraction_parsed",
            extra={
                "workflow_id": workflow_id,
                "artifact_id": artifact.artifact_id,
                "entity_count": len(filtered.entities),
                "relation_count": len(filtered.relations),
                "dropped_entity_types": filtered.dropped_entity_types,
                "dropped_relation_types": filtered.dropped_relation_types,
                "dropped_dangling_relations": filtered.dropped_dangling_relations,
            },
        )
        return EntityGraph(
            source_artifact_id=artifact.artifact_id,
            ontology_version=self.config.ontology.ontology_version,
            extractor_model_id=self.extractor_model_id,
            entities=filtered.entities,
            relations=filtered.relations,
            truncated=filtered.truncated,
        )

    def _extract_window(
        self,
        *,
        artifact: ArtifactRef,
        window: str,
        workflow_id: str,
    ) -> tuple[list[ExtractedEntity], list[ExtractedRelation]]:
        prompt = build_extraction_prompt(self.config.ontology, window)
        raw_text = self._call_extractor(
            artifact=artifact,
            prompt=prompt,
            workflow_id=workflow_id,
        )
        try:
            return parse_extraction_payload(decode_extraction_json(raw_text))
        except MalformedExtractionError:
            logger.warning(
                "graph_extraction_malformed_retrying",
                extra={"workflow_id": workflow_id, "artifact_id": artifact.artifact_id},
            )
        repair_prompt = (
            "Your previous reply was not a JSON object and could not be used.\n"
            "Reply with the JSON object only: no prose, no code fence, no commentary.\n\n"
            f"{prompt}"
        )
        repaired = self._call_extractor(
            artifact=artifact,
            prompt=repair_prompt,
            workflow_id=workflow_id,
        )
        return parse_extraction_payload(decode_extraction_json(repaired))

    def _call_extractor(self, *, artifact: ArtifactRef, prompt: str, workflow_id: str) -> str:
        result = self.model_manager.call_model(
            ModelCallRequest(
                workflow_id=workflow_id,
                model_role=self.config.extraction.extractor_role,
                input_artifact_id=artifact.artifact_id,
                payload={"prompt": prompt},
                params={"temperature": 0, "max_tokens": 4096, "cache_prompt": False},
                timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
            )
        )
        raw = self.artifact_store.read_json(result.output_artifact.artifact_id)
        return str(raw["output"].get("text", ""))

    def merge(
        self,
        graph: EntityGraph,
        *,
        chunk_id: str | None = None,
        workflow_id: str | None = None,
    ) -> MergeOutcome:
        """Resolve and upsert one extraction into the graph.

        Every write keys on a stable hash, so re-running this against the same
        `entity_graph.v1` artifact converges rather than duplicating. That is
        what makes both crash recovery and `pi /graph rebuild` safe.
        """
        outcome = MergeOutcome()
        embeddings = self._resolution_embeddings(
            graph.entities,
            outcome,
            artifact_id=graph.source_artifact_id,
            workflow_id=workflow_id,
        )
        review_threshold = self.config.extraction.review_threshold
        node_ids_by_name: dict[str, list[str]] = {}

        for entity in graph.entities:
            normalized = normalize_entity_name(entity.name)
            resolved_id, path = self.repository.resolve_graph_node(
                node_type=entity.node_type,
                normalized_name=normalized,
                embedding=embeddings.get(normalized),
                resolution_threshold=self.config.extraction.resolution_threshold,
            )
            node_id = resolved_id or build_graph_node_id(entity.node_type, normalized)
            write = self.repository.upsert_graph_node(
                node_id=node_id,
                node_type=entity.node_type,
                canonical_name=entity.name,
                normalized_name=normalized,
                artifact_id=graph.source_artifact_id,
                embedding=embeddings.get(normalized),
                properties=entity.properties,
                needs_review=entity.confidence < review_threshold,
                alias=entity.name,
            )
            node_ids_by_name.setdefault(normalized, []).append(node_id)
            if write is GraphWriteOutcome.CREATED:
                outcome.nodes_created += 1
                GRAPH_NODES_CREATED_TOTAL.labels(
                    workflow_type=WorkflowType.GRAPH_EXTRACTION.value
                ).inc()
            else:
                outcome.nodes_merged += 1
                GRAPH_NODES_MERGED_TOTAL.labels(
                    workflow_type=WorkflowType.GRAPH_EXTRACTION.value
                ).inc()
            if path is NodeResolutionPath.EMBEDDING:
                outcome.resolution_collisions += 1
                GRAPH_RESOLUTION_COLLISIONS_TOTAL.labels(
                    workflow_type=WorkflowType.GRAPH_EXTRACTION.value
                ).inc()
            logger.info(
                "graph_node_written",
                extra={
                    "workflow_id": workflow_id,
                    "artifact_id": graph.source_artifact_id,
                    "node_id": node_id,
                    "outcome": write.value,
                    "resolution_path": path.value,
                },
            )
            if self.repository.upsert_graph_mention(
                mention_id=build_graph_mention_id(node_id, graph.source_artifact_id, chunk_id),
                node_id=node_id,
                artifact_id=graph.source_artifact_id,
                chunk_id=chunk_id,
                snippet=entity.snippet,
            ):
                outcome.mentions_created += 1

        for relation in graph.relations:
            src_candidates = node_ids_by_name.get(normalize_entity_name(relation.src_name), [])
            dst_candidates = node_ids_by_name.get(normalize_entity_name(relation.dst_name), [])
            if len(src_candidates) != 1 or len(dst_candidates) != 1:
                logger.warning(
                    "graph_relation_ambiguous_endpoint",
                    extra={
                        "workflow_id": workflow_id,
                        "artifact_id": graph.source_artifact_id,
                        "src_candidate_count": len(src_candidates),
                        "dst_candidate_count": len(dst_candidates),
                    },
                )
                continue
            src_id = src_candidates[0]
            dst_id = dst_candidates[0]
            if src_id == dst_id:
                continue
            edge_id = build_graph_edge_id(src_id, relation.edge_type, dst_id)
            write = self.repository.upsert_graph_edge(
                edge_id=edge_id,
                src_node_id=src_id,
                dst_node_id=dst_id,
                edge_type=relation.edge_type,
                confidence=relation.confidence,
                artifact_id=graph.source_artifact_id,
                needs_review=relation.confidence < review_threshold,
            )
            if write is GraphWriteOutcome.CREATED:
                outcome.edges_created += 1
            else:
                outcome.edges_merged += 1
            logger.info(
                "graph_edge_written",
                extra={
                    "workflow_id": workflow_id,
                    "artifact_id": graph.source_artifact_id,
                    "edge_id": edge_id,
                    "outcome": write.value,
                },
            )
        return outcome

    def _resolution_embeddings(
        self,
        entities: list[ExtractedEntity],
        outcome: MergeOutcome,
        *,
        artifact_id: str,
        workflow_id: str | None,
    ) -> dict[str, list[float]]:
        """Embed entity names for fuzzy resolution, or degrade cleanly.

        §12 makes the embedder optional: without it, resolution falls back to
        exact `normalized_name` matching and the run still completes. The
        degradation is recorded rather than inferred, so a graph built without
        fuzzy merging is distinguishable after the fact.
        """
        names = sorted({normalize_entity_name(entity.name) for entity in entities if entity.name})
        if not names:
            return {}
        try:
            self.model_manager.require_loaded(ModelRole.EMBEDDER)
            vectors = self.model_manager.embed_texts(names, "graph-resolution")
        except (ModelNotLoadedError, OSError) as exc:
            logger.warning(
                "graph_resolution_degraded",
                extra={
                    "workflow_id": workflow_id,
                    "artifact_id": artifact_id,
                    "reason": str(exc),
                },
            )
            outcome.resolution_degraded = True
            return {}
        from .retrieval import truncate_embedding

        return {
            name: truncate_embedding(vector) for name, vector in zip(names, vectors, strict=True)
        }
