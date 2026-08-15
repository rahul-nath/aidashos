# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Knowledge graph workflows: extraction, analytics, and the /graph directive.

Two workflow types with different units of work live here. Extraction's unit is
one artifact, which is what makes a batch survive a single bad note. Analytics'
unit is the whole graph, because centrality and communities are global.

The status a run ends in is the contract the §4 failure table specifies:
a model that might work later is FAILED_RETRYABLE, output that will never parse
is FAILED_PERMANENT with the raw text kept, and an artifact with nothing to
extract is a COMPLETED no-op rather than an error.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

from ..artifacts import ArtifactStore
from ..contracts import (
    ArtifactRef,
    ArtifactRole,
    DirectiveSpec,
    EntityGraph,
    GraphConfig,
    GraphSubcommand,
    IngressEvent,
    SourceType,
    Stage,
    WorkflowResult,
    WorkflowStatus,
    WorkflowType,
    WorkspaceId,
    WorkspacePolicy,
)
from ..graph_analytics import run_graph_analytics
from ..graph_extraction import (
    GraphExtractor,
    MalformedExtractionError,
    MergeOutcome,
)
from ..ids import build_event_id, build_graph_extraction_workflow_id
from ..model_manager import ModelNotLoadedError
from ..observability import (
    GRAPH_ANALYTICS_LATENCY_SECONDS,
    GRAPH_EXTRACTION_LATENCY_SECONDS,
)
from ..retrieval import EMBEDDABLE_ROLES
from .base import WorkflowMixinBase
from .core import build_completed_workflow_result

logger = logging.getLogger(__name__)

# The graph is derived from artifacts, not from a watched directory, so every
# graph run is operator-initiated and lands in the general workspace.
GRAPH_WORKSPACE_ID = WorkspaceId.GENERAL.value


class GraphBatchTooLargeError(ValueError):
    """A `/graph build` batch exceeded max_batch_artifacts.

    Raised before any extraction runs: rejecting a 900-artifact batch after
    400 model calls would be the expensive way to enforce a bound.
    """


class GraphPathOutsidePolicyError(PermissionError):
    """`/graph build <path>` pointed outside the workspace policy roots."""


class GraphWorkflowMixin(WorkflowMixinBase):
    artifact_store: ArtifactStore

    # -- configuration -------------------------------------------------

    def graph_config(self) -> GraphConfig:
        """Read `configs/ontology.toml` fresh on each run.

        Not cached on purpose: the documented recovery for both over-merging
        and junk extraction is to edit a threshold and re-run, and a cached
        config would make that require a restart.
        """
        return GraphConfig.from_toml(
            self.runtime.settings.load_toml(self.runtime.settings.ontology_path)
        )

    def _graph_extractor(self, config: GraphConfig) -> GraphExtractor:
        return GraphExtractor(
            repository=self.runtime.repository,
            artifact_store=self.runtime.artifact_store,
            model_manager=self.runtime.model_manager,
            config=config,
        )

    # -- extraction ----------------------------------------------------

    def graph_extraction(self, event: IngressEvent) -> WorkflowResult:
        """Extract one artifact into the graph.

        The artifact is named in the event payload rather than inferred, so a
        replayed ingress event always re-runs the same unit of work.
        """
        artifact_id = str(event.payload.get("artifact_id", ""))
        artifact = self.runtime.repository.get_artifact(artifact_id)
        if artifact is None:
            raise KeyError(f"Missing graph extraction artifact: {artifact_id}")
        return self.extract_artifact_into_graph(artifact, event=event)

    def extract_artifact_into_graph(
        self,
        artifact: ArtifactRef,
        *,
        event: IngressEvent | None = None,
        config: GraphConfig | None = None,
    ) -> WorkflowResult:
        config = config or self.graph_config()
        extractor = self._graph_extractor(config)
        workflow_id = build_graph_extraction_workflow_id(
            artifact.artifact_id,
            config.ontology.ontology_version,
            extractor.extractor_model_id,
        )

        skip_reason = self._graph_extraction_skip_reason(artifact)
        if skip_reason is not None:
            return self._completed_no_op(workflow_id, artifact, event, skip_reason)

        # §9: the workflow id folds the artifact, ontology version, and model
        # together, so an unchanged re-run is a skip rather than a second merge.
        existing_state = self.runtime.repository.get_workflow_run_state(workflow_id)
        if existing_state is not None and existing_state.status is WorkflowStatus.COMPLETED:
            logger.info(
                "graph_extraction_skipped_completed",
                extra={"workflow_id": workflow_id, "artifact_id": artifact.artifact_id},
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.GRAPH_EXTRACTION,
                WorkflowStatus.COMPLETED,
                Stage.COMPLETED,
                [],
            )

        if existing_state is None:
            self._start_graph_run(workflow_id, WorkflowType.GRAPH_EXTRACTION, event)
        elif existing_state.status is WorkflowStatus.FAILED_RETRYABLE:
            self._resume_graph_run(workflow_id)
        else:
            logger.info(
                "graph_extraction_existing_terminal_or_active",
                extra={
                    "workflow_id": workflow_id,
                    "artifact_id": artifact.artifact_id,
                    "status": existing_state.status.value,
                },
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.GRAPH_EXTRACTION,
                existing_state.status,
                existing_state.current_stage,
                [],
                manual_review_reason=existing_state.last_error,
            )
        text = self._artifact_text(artifact)

        try:
            with GRAPH_EXTRACTION_LATENCY_SECONDS.labels(
                workflow_type=WorkflowType.GRAPH_EXTRACTION.value
            ).time():
                graph = extractor.extract(
                    artifact=artifact,
                    text=text,
                    workflow_id=workflow_id,
                )
        except MalformedExtractionError as exc:
            # The raw output is already durable as a `model_output` artifact
            # written by call_model, which is what §1.3 requires before any
            # terminal transition. No amount of retrying will parse it.
            self.runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_PERMANENT,
                stage=Stage.COMPLETED,
                error=str(exc),
            )
            logger.warning(
                "graph_extraction_failed_permanent",
                extra={"workflow_id": workflow_id, "artifact_id": artifact.artifact_id},
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.GRAPH_EXTRACTION,
                WorkflowStatus.FAILED_PERMANENT,
                Stage.COMPLETED,
                self.runtime.repository.list_workflow_artifacts(
                    workflow_id,
                    roles=[ArtifactRole.MODEL_OUTPUT.value],
                ),
                manual_review_reason=str(exc),
            )
        except (ModelNotLoadedError, OSError, TimeoutError, httpx.HTTPError) as exc:
            # A model that is down now may be up later, and nothing has been
            # written to the graph, so the run is safe to retry wholesale.
            self.runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_RETRYABLE,
                stage=Stage.PROCESSING,
                error=str(exc),
                retry_increment=True,
            )
            logger.warning(
                "graph_extraction_failed_retryable",
                extra={"workflow_id": workflow_id, "artifact_id": artifact.artifact_id},
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.GRAPH_EXTRACTION,
                WorkflowStatus.FAILED_RETRYABLE,
                Stage.PROCESSING,
                [],
                manual_review_reason=str(exc),
            )

        graph_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.ENTITY_GRAPH.value,
            payload=graph.model_dump(),
            workflow_id=workflow_id,
            schema_version=graph.schema_version,
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.PROCESSING,
            stage=Stage.ARTIFACT_PERSISTED,
        )

        # The merge is EGRESS_PENDING because the graph is a derived store
        # written with egress-like idempotency discipline, not a second truth.
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.PROCESSING,
            stage=Stage.EGRESS_PENDING,
        )
        try:
            outcome = extractor.merge(graph, workflow_id=workflow_id)
        except (OSError, SQLAlchemyError) as exc:
            self.runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_RETRYABLE,
                stage=Stage.EGRESS_PENDING,
                error=str(exc),
                retry_increment=True,
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.GRAPH_EXTRACTION,
                WorkflowStatus.FAILED_RETRYABLE,
                Stage.EGRESS_PENDING,
                [graph_artifact],
                manual_review_reason=str(exc),
            )

        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.COMPLETED,
            stage=Stage.COMPLETED,
            clear_error=True,
        )
        logger.info(
            "graph_extraction_completed",
            extra={
                "workflow_id": workflow_id,
                "artifact_id": artifact.artifact_id,
                "nodes_created": outcome.nodes_created,
                "nodes_merged": outcome.nodes_merged,
                "edges_created": outcome.edges_created,
                "resolution_degraded": outcome.resolution_degraded,
            },
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.GRAPH_EXTRACTION,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            [graph_artifact],
            embedding_degraded=outcome.resolution_degraded,
        )

    def _graph_extraction_skip_reason(self, artifact: ArtifactRef) -> str | None:
        """Why this artifact carries no extractable entities, if it does not.

        Both cases are no-ops rather than failures: a screenshot has no text to
        extract, and a medical artifact the policy has not opted in is a
        deliberate exclusion, not a broken run.
        """
        if str(artifact.role) not in EMBEDDABLE_ROLES:
            return f"role {artifact.role} is not an embeddable text role"
        if self._is_medical_artifact(artifact) and not self._medical_policy().embed_medical_outputs:
            return "medical workspace has not opted into embedding its outputs"
        return None

    def _is_medical_artifact(self, artifact: ArtifactRef) -> bool:
        return str(
            artifact.role
        ) == ArtifactRole.MED_REPORT.value or artifact.schema_version.startswith("med_report.")

    def _medical_policy(self) -> WorkspacePolicy:
        return self.runtime.policy_store.get(WorkspaceId.MEDICAL.value)

    def _artifact_text(self, artifact: ArtifactRef) -> str:
        return self.runtime.artifact_store.local_path(artifact).read_text(
            encoding="utf-8",
            errors="replace",
        )

    def _completed_no_op(
        self,
        workflow_id: str,
        artifact: ArtifactRef,
        event: IngressEvent | None,
        reason: str,
    ) -> WorkflowResult:
        if not self.runtime.repository.workflow_run_exists(workflow_id):
            self._start_graph_run(workflow_id, WorkflowType.GRAPH_EXTRACTION, event)
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.COMPLETED,
            stage=Stage.COMPLETED,
        )
        logger.info(
            "graph_extraction_no_op",
            extra={
                "workflow_id": workflow_id,
                "artifact_id": artifact.artifact_id,
                "reason": reason,
            },
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.GRAPH_EXTRACTION,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            [],
        )

    def _start_graph_run(
        self,
        workflow_id: str,
        workflow_type: WorkflowType,
        event: IngressEvent | None,
    ) -> None:
        if event is not None:
            self.runtime.repository.register_ingress_event(event)
        self.runtime.repository.start_workflow_run(
            workflow_id=workflow_id,
            workflow_type=workflow_type.value,
            workspace_id=GRAPH_WORKSPACE_ID,
            input_event_id=event.event_id if event is not None else None,
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.PROCESSING,
            stage=Stage.VALIDATED,
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.PROCESSING,
            stage=Stage.PROCESSING,
        )

    def _resume_graph_run(self, workflow_id: str) -> None:
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.PROCESSING,
            stage=Stage.VALIDATED,
            clear_error=True,
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.PROCESSING,
            stage=Stage.PROCESSING,
        )

    # -- batch ---------------------------------------------------------

    def build_graph_over_artifacts(
        self,
        *,
        path: Path | None = None,
        config: GraphConfig | None = None,
    ) -> list[WorkflowResult]:
        """Extract every candidate artifact, one independent run each.

        A failure in one run is recorded and the batch continues: the §3 unit
        of work is the artifact precisely so that one bad note cannot cost the
        other 499.
        """
        config = config or self.graph_config()
        artifacts = self._batch_artifacts(path=path, config=config)
        results: list[WorkflowResult] = []
        for artifact in artifacts:
            results.append(self.extract_artifact_into_graph(artifact, config=config))
        return results

    def _batch_artifacts(
        self,
        *,
        path: Path | None,
        config: GraphConfig,
    ) -> list[ArtifactRef]:
        if path is not None:
            self._ensure_path_within_policy(path)
        artifacts = self.runtime.repository.list_artifacts_by_role(sorted(EMBEDDABLE_ROLES))
        if path is not None:
            root = path.expanduser().resolve()
            artifacts = [
                ref
                for ref in artifacts
                if (candidate := Path(ref.path).resolve()) == root or root in candidate.parents
            ]
        limit = config.extraction.max_batch_artifacts
        if len(artifacts) > limit:
            raise GraphBatchTooLargeError(
                f"/graph build batch of {len(artifacts)} artifacts exceeds "
                f"max_batch_artifacts={limit}. Narrow the path or raise the limit "
                f"in configs/ontology.toml."
            )
        return artifacts

    def _ensure_path_within_policy(self, path: Path) -> None:
        """Keep `/graph build` inside the workspace roots the policy names.

        §13: extraction reads whatever it is pointed at, so the path check is
        the boundary, not the model.
        """
        resolved = path.expanduser().resolve()
        roots = [
            policy.root_path.expanduser().resolve() for policy in self.runtime.policy_store.all()
        ]
        if any(resolved == root or root in resolved.parents for root in roots):
            return
        raise GraphPathOutsidePolicyError(
            f"/graph build refused {resolved}: outside every workspace policy root."
        )

    # -- analytics -----------------------------------------------------

    def graph_analytics(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start_analytics_run(event)
        with GRAPH_ANALYTICS_LATENCY_SECONDS.labels(
            workflow_type=WorkflowType.GRAPH_ANALYTICS.value
        ).time():
            metrics = run_graph_analytics(self.runtime.repository)
        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.GRAPH_METRICS.value,
            payload=metrics.model_dump(),
            workflow_id=workflow_id,
            schema_version=metrics.schema_version,
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.COMPLETED,
            stage=Stage.COMPLETED,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.GRAPH_ANALYTICS,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            [artifact],
        )

    def _start_analytics_run(self, event: IngressEvent) -> str:
        workflow_id = self._start(WorkflowType.GRAPH_ANALYTICS, event)
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.PROCESSING,
            stage=Stage.PROCESSING,
        )
        return workflow_id

    # -- rebuild -------------------------------------------------------

    def rebuild_graph(self, *, config: GraphConfig | None = None) -> MergeOutcome:
        """Drop the graph and re-derive it from the `entity_graph.v1` artifacts.

        This is the canonical replay and the documented recovery for every
        resolution mistake: raise a threshold, rebuild, and the bad merge is
        gone. It never calls a model, because the extraction outputs are
        already durable; only resolution is recomputed.
        """
        config = config or self.graph_config()
        extractor = self._graph_extractor(config)
        graphs_by_source: dict[str, EntityGraph] = {}
        for ref in self.runtime.repository.list_artifacts_by_role(
            [ArtifactRole.ENTITY_GRAPH.value],
            limit=None,
        ):
            payload = self.runtime.artifact_store.read_json(ref.artifact_id)
            graph = EntityGraph.model_validate(payload)
            if graph.ontology_version != config.ontology.ontology_version:
                continue
            if graph.extractor_model_id != extractor.extractor_model_id:
                continue
            graphs_by_source[graph.source_artifact_id] = graph

        graphs = [
            graphs_by_source[source_artifact_id] for source_artifact_id in sorted(graphs_by_source)
        ]
        self.runtime.repository.drop_graph()
        total = MergeOutcome()
        for graph in graphs:
            outcome = extractor.merge(graph)
            total.nodes_created += outcome.nodes_created
            total.nodes_merged += outcome.nodes_merged
            total.edges_created += outcome.edges_created
            total.edges_merged += outcome.edges_merged
            total.mentions_created += outcome.mentions_created
            total.resolution_collisions += outcome.resolution_collisions
            total.resolution_degraded = total.resolution_degraded or outcome.resolution_degraded
        logger.info(
            "graph_rebuild_completed",
            extra={"nodes_created": total.nodes_created, "edges_created": total.edges_created},
        )
        return total

    # -- directive -----------------------------------------------------

    def _graph_directive(self, event: IngressEvent, spec: DirectiveSpec) -> dict[str, Any]:
        """Route one `/graph` subcommand to its workflow or its read model."""
        subcommand = spec.graph_subcommand
        if subcommand is None:
            raise ValueError("/graph requires a subcommand.")
        config = self.graph_config()

        if subcommand is GraphSubcommand.BUILD:
            results = self.build_graph_over_artifacts(path=spec.path, config=config)
            return {
                "subcommand": subcommand.value,
                "artifacts_processed": len(results),
                "completed": sum(1 for r in results if r.status is WorkflowStatus.COMPLETED),
                "failed": sum(1 for r in results if r.status is not WorkflowStatus.COMPLETED),
            }
        if subcommand is GraphSubcommand.ANALYZE:
            result = self.graph_analytics(self._graph_event("graph_analyze"))
            return {"subcommand": subcommand.value, "workflow_id": result.workflow_id}
        if subcommand is GraphSubcommand.REBUILD:
            outcome = self.rebuild_graph(config=config)
            return {
                "subcommand": subcommand.value,
                "nodes": outcome.nodes_created,
                "edges": outcome.edges_created,
            }
        if subcommand is GraphSubcommand.STATS:
            return {"subcommand": subcommand.value, **self.runtime.repository.graph_stats()}
        if subcommand is GraphSubcommand.REVIEW:
            return {
                "subcommand": subcommand.value,
                "nodes": [
                    node.model_dump()
                    for node in self.runtime.repository.list_graph_nodes(needs_review=True)
                ],
                "edges": [
                    edge.model_dump()
                    for edge in self.runtime.repository.list_graph_edges(needs_review=True)
                ],
            }
        if subcommand is GraphSubcommand.NODE:
            return self._graph_node_report(str(spec.query or ""), config)
        return self._graph_get_report(str(spec.query or ""), config)

    def _graph_node_report(self, name: str, config: GraphConfig) -> dict[str, Any]:
        from ..graph_extraction import normalize_entity_name

        normalized = normalize_entity_name(name)
        matches = [
            node
            for node in self.runtime.repository.list_graph_nodes()
            if node.normalized_name == normalized
        ]
        neighborhood = self.runtime.repository.graph_neighborhood(
            seed_node_ids=[node.node_id for node in matches],
            max_hops=1,
            max_neighbors=config.retrieval.max_neighbors,
        )
        return {
            "subcommand": GraphSubcommand.NODE.value,
            "query": name,
            "matches": [node.model_dump() for node in matches],
            "neighborhood": neighborhood.model_dump(),
        }

    def _graph_get_report(self, query: str, config: GraphConfig) -> dict[str, Any]:
        hits, neighborhood = self.runtime.retrieval.graph_augmented_context(
            query,
            retrieval_bounds=config.retrieval,
        )
        return {
            "subcommand": GraphSubcommand.GET.value,
            "query": query,
            "hits": [
                {"chunk_id": hit.chunk_id, "text": hit.text, "score": hit.score} for hit in hits
            ],
            "neighborhood": neighborhood.model_dump(),
        }

    def _graph_event(self, event_type: str) -> IngressEvent:
        source_uri = f"graph://{event_type}"
        return IngressEvent(
            event_id=build_event_id(
                SourceType.MANUAL,
                GRAPH_WORKSPACE_ID,
                source_uri,
                event_type,
                None,
            ),
            source_type=SourceType.MANUAL,
            event_type=event_type,
            workspace_id=GRAPH_WORKSPACE_ID,
            source_uri=source_uri,
        )
