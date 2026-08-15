# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whole-graph structural analytics.

Analytics is the one stage whose unit of work is the entire graph rather than
one artifact, because PageRank and community detection are global by nature.
It runs in-process over networkx and writes its scores back onto the nodes, so
the graph stays the only place retrieval has to look.

Everything computed here is derived twice over: from the graph, which is itself
derived from the `entity_graph.v1` artifacts. Losing it costs one re-run.
"""

from __future__ import annotations

import logging

import networkx as nx

from .contracts import GraphMetrics
from .repository import Repository

logger = logging.getLogger(__name__)

# PageRank's usual damping factor. Named rather than inlined because it is a
# tuning knob an operator may reasonably want to find.
PAGERANK_DAMPING = 0.85
PAGERANK_MAX_ITERATIONS = 100
PAGERANK_TOLERANCE = 1.0e-6


def build_networkx_graph(repository: Repository) -> nx.Graph:
    """Load the graph as undirected for structural analysis.

    Direction carries meaning for retrieval but not for centrality or
    community structure at this scale: "Rahul authored the OS" and "the OS was
    authored by Rahul" are one structural fact, and treating them as two would
    split communities that are really one.
    """
    graph = nx.Graph()
    for node in repository.list_graph_nodes():
        graph.add_node(node.node_id)
    for edge in repository.list_graph_edges():
        if graph.has_node(edge.src_node_id) and graph.has_node(edge.dst_node_id):
            graph.add_edge(edge.src_node_id, edge.dst_node_id, weight=float(edge.weight))
    return graph


def compute_communities(graph: nx.Graph) -> dict[str, int]:
    """Assign every node a community id, isolated nodes included.

    Louvain is seeded so repeated analytics passes over an unchanged graph
    produce the same partition; an unstable community id would make the
    `graph_metrics.v1` history meaningless.
    """
    if graph.number_of_nodes() == 0:
        return {}
    communities = nx.community.louvain_communities(graph, seed=0)
    assignment: dict[str, int] = {}
    for community_id, members in enumerate(sorted(communities, key=lambda m: sorted(m))):
        for node_id in members:
            assignment[str(node_id)] = community_id
    return assignment


def compute_pagerank(graph: nx.Graph) -> dict[str, float]:
    """Compute weighted PageRank without making SciPy a runtime dependency."""
    node_count = graph.number_of_nodes()
    if node_count == 0:
        return {}

    nodes = list(graph.nodes)
    ranks = {node_id: 1.0 / node_count for node_id in nodes}
    weighted_degrees = {
        node_id: sum(float(data.get("weight", 1.0)) for data in graph[node_id].values())
        for node_id in nodes
    }
    base_rank = (1.0 - PAGERANK_DAMPING) / node_count

    for _ in range(PAGERANK_MAX_ITERATIONS):
        dangling_rank = sum(ranks[node_id] for node_id in nodes if weighted_degrees[node_id] == 0.0)
        next_ranks = {
            node_id: base_rank + PAGERANK_DAMPING * dangling_rank / node_count for node_id in nodes
        }
        for source_id in nodes:
            source_degree = weighted_degrees[source_id]
            if source_degree == 0.0:
                continue
            for target_id, data in graph[source_id].items():
                edge_weight = float(data.get("weight", 1.0))
                next_ranks[target_id] += (
                    PAGERANK_DAMPING * ranks[source_id] * edge_weight / source_degree
                )
        if sum(abs(next_ranks[node_id] - ranks[node_id]) for node_id in nodes) < (
            node_count * PAGERANK_TOLERANCE
        ):
            return {str(node_id): rank for node_id, rank in next_ranks.items()}
        ranks = next_ranks

    raise RuntimeError(f"PageRank failed to converge after {PAGERANK_MAX_ITERATIONS} iterations")


def run_graph_analytics(repository: Repository, *, top_n: int = 5) -> GraphMetrics:
    """Compute and persist pagerank, degree, and community for every node."""
    graph = build_networkx_graph(repository)
    node_count = graph.number_of_nodes()
    edge_count = graph.number_of_edges()
    if node_count == 0:
        logger.info("graph_analytics_empty")
        return GraphMetrics(node_count=0, edge_count=0, community_count=0, top_nodes=[])

    ranks = compute_pagerank(graph)
    degrees = dict(graph.degree())
    communities = compute_communities(graph)

    for node_id in graph.nodes:
        repository.set_graph_node_metrics(
            node_id=str(node_id),
            pagerank=float(ranks.get(node_id, 0.0)),
            degree=int(degrees.get(node_id, 0)),
            community_id=int(communities.get(str(node_id), 0)),
        )

    stats = repository.graph_stats(top_n=top_n)
    logger.info(
        "graph_analytics_completed",
        extra={"node_count": node_count, "edge_count": edge_count},
    )
    return GraphMetrics(
        node_count=node_count,
        edge_count=edge_count,
        community_count=len(set(communities.values())),
        top_nodes=stats["top_nodes"],
    )
