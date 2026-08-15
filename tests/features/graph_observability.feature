# Traces knowledge_graph_layer_gawd_doc_v1.md
#   expected metrics §1.3
#   observability    §10
#   SLO targets      §1.5

Feature: What the graph layer reports about itself

  Every metric named in the doc is emitted, labelled by workflow_type, so the
  runbook hooks in §10 have something to fire on.

  Background:
    Given the ontology declares the entity types:
      | type   | description                          |
      | Person | A named individual                    |
      | Tool   | A software tool, library, or service  |
    And the ontology declares the relation types:
      | type       | description                |
      | DEPENDS_ON | Source requires the target |

  @doc-1.3 @metrics
  Scenario: An extraction run emits its documented metrics
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul depends on DBOS.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
      | DBOS  | Tool      | 0.90       |
    And the extractor returns the relations:
      | src_name | edge_type  | dst_name | confidence |
      | Rahul    | DEPENDS_ON | DBOS     | 0.87       |
    When I extract the graph from "note-alpha"
    Then the metric "graph_extraction_latency_seconds" was observed
    And the metric "graph_entities_extracted" recorded 2
    And the metric "graph_relations_extracted" recorded 1
    And the metric "graph_nodes_created_total" recorded 2
    And the metric "graph_nodes_merged_total" recorded 0
    And every metric observed in this run is labelled "graph_extraction"

  @doc-1.3 @metrics
  Scenario: Merging an existing node is counted as a merge, not a create
    Given the graph already contains a "Person" node named "Rahul"
    And an embeddable artifact "note-alpha" with the text:
      """
      Rahul again.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
    When I extract the graph from "note-alpha"
    Then the metric "graph_nodes_created_total" recorded 0
    And the metric "graph_nodes_merged_total" recorded 1
    And every metric observed in this run is labelled "graph_extraction"

  @doc-1.3 @doc-10 @metrics
  Scenario: An embedding-similarity merge is counted as a resolution collision
    Given the ontology sets "resolution_threshold" to 0.86
    And the graph already contains a "Person" node named "Rahul Nath"
    And "R Nath" has embedding similarity 0.95 to "Rahul Nath"
    And an embeddable artifact "note-alpha" with the text:
      """
      R Nath again.
      """
    And the extractor returns the entities:
      | name   | node_type | confidence |
      | R Nath | Person    | 0.95       |
    When I extract the graph from "note-alpha"
    Then the metric "graph_resolution_collisions_total" recorded 1
    And every metric observed in this run is labelled "graph_extraction"

  @doc-1.3 @metrics
  Scenario: An analytics run reports its latency
    Given the graph holds:
      | src   | edge_type  | dst  |
      | Rahul | DEPENDS_ON | DBOS |
    When I run graph analytics
    Then the metric "graph_analytics_latency_seconds" was observed
    And every metric observed in this run is labelled "graph_analytics"

  @doc-1.3 @metrics
  Scenario: A graph-augmented query reports its latency
    Given the graph holds:
      | src   | edge_type  | dst  |
      | Rahul | DEPENDS_ON | DBOS |
    And the chunk "chunk-1" mentions the node "Rahul"
    And a vector search for "anything" returns "chunk-1"
    When I ask the graph "anything"
    Then the metric "graph_augmented_query_latency_seconds" was observed
    And every metric observed in this run is labelled "model_directive"

  @doc-1.3 @logging
  Scenario: Every log line from a run carries its workflow and artifact
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul depends on DBOS.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
    When I extract the graph from "note-alpha"
    Then every graph log line carries the workflow id
    And every graph log line carries the artifact id
