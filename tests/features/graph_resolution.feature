# Traces knowledge_graph_layer_gawd_doc_v1.md
#   resolution + merge  §1.3 step 8
#   over-merge scenario §8
#   idempotency/replay  §9
#   SLO: re-run determinism §1.5

Feature: Resolving an extracted entity onto the graph

  Resolution is what collapses "GAWD doc" and "the gawd doc" into one node.
  It resolves within a node_type first by exact normalized_name, then by
  embedding similarity above resolution_threshold. Every merge is idempotent
  because node_id is hash(node_type + normalized_name).

  Background:
    Given the ontology declares the entity types:
      | type    | description                          |
      | Person  | A named individual                   |
      | Concept | An idea, technique, or topic         |
      | Tool    | A software tool, library, or service |
    And the ontology declares the relation types:
      | type       | description                |
      | DEPENDS_ON | Source requires the target |

  @doc-1.3 @resolution
  Scenario: Surface forms differing only by case and punctuation are the same node
    Given the graph already contains a "Concept" node named "GAWD doc"
    And an embeddable artifact "note-alpha" with the text:
      """
      The GAWD Doc. explains it.
      """
    And the extractor returns the entities:
      | name          | node_type | confidence |
      | The GAWD Doc. | Concept   | 0.90       |
    When I extract the graph from "note-alpha"
    Then the graph has 1 nodes and 0 edges
    And the node "GAWD doc" was merged by the "exact" path
    And the node "GAWD doc" has aliases "The GAWD Doc."

  @doc-1.3 @resolution
  Scenario: A near-duplicate above the similarity threshold merges
    Given the ontology sets "resolution_threshold" to 0.86
    And the graph already contains a "Concept" node named "GAWD doc"
    And "the gawd document" has embedding similarity 0.93 to "GAWD doc"
    And an embeddable artifact "note-alpha" with the text:
      """
      See the gawd document.
      """
    And the extractor returns the entities:
      | name              | node_type | confidence |
      | the gawd document | Concept   | 0.90       |
    When I extract the graph from "note-alpha"
    Then the graph has 1 nodes and 0 edges
    And the node "GAWD doc" was merged by the "embedding" path

  @doc-8 @resolution
  Scenario: A near-duplicate below the similarity threshold stays separate
    Given the ontology sets "resolution_threshold" to 0.86
    And the graph already contains a "Concept" node named "GAWD doc"
    And "the design doc" has embedding similarity 0.70 to "GAWD doc"
    And an embeddable artifact "note-alpha" with the text:
      """
      See the design doc.
      """
    And the extractor returns the entities:
      | name           | node_type | confidence |
      | the design doc | Concept   | 0.90       |
    When I extract the graph from "note-alpha"
    Then the graph has 2 nodes and 0 edges
    And the node "the design doc" was created

  @doc-8 @resolution
  Scenario: Resolution never crosses node types, however similar the names
    Given the ontology sets "resolution_threshold" to 0.86
    And the graph already contains a "Person" node named "Atlas"
    And "Atlas" has embedding similarity 0.99 to "Atlas"
    And an embeddable artifact "note-alpha" with the text:
      """
      Atlas is the deployment tool.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Atlas | Tool      | 0.90       |
    When I extract the graph from "note-alpha"
    Then the graph has 2 nodes and 0 edges
    And the graph contains a "Person" node named "Atlas"
    And the graph contains a "Tool" node named "Atlas"

  @doc-9 @idempotency @slo-rerun-determinism
  Scenario: Extracting the same artifact twice changes nothing
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
    And I extract the graph from "note-alpha" again
    Then the graph has 2 nodes and 1 edges
    And the node "Rahul" has 1 mention of "note-alpha"
    And the second run was skipped as already completed

  @doc-9 @idempotency
  Scenario: A changed ontology version re-derives rather than deduplicating away
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul depends on DBOS.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
    When I extract the graph from "note-alpha"
    And the ontology version becomes "v2"
    And I extract the graph from "note-alpha" again
    Then the second run was not skipped
    And the graph has 1 nodes and 0 edges

  @doc-9 @idempotency
  Scenario: The same entity seen in two artifacts is one node with two mentions
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul writes notes.
      """
    And an embeddable artifact "note-beta" with the text:
      """
      Rahul reviews notes.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
    When I extract the graph from "note-alpha"
    And I extract the graph from "note-beta"
    Then the graph has 1 nodes and 0 edges
    And the node "Rahul" has 1 mention of "note-alpha"
    And the node "Rahul" has 1 mention of "note-beta"
    And the node "Rahul" has mention count 2

  @doc-9 @idempotency
  Scenario: Re-asserting an edge raises its weight and keeps the highest confidence
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul depends on DBOS.
      """
    And an embeddable artifact "note-beta" with the text:
      """
      Rahul really depends on DBOS.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
      | DBOS  | Tool      | 0.90       |
    And the extractor returns the relations:
      | src_name | edge_type  | dst_name | confidence |
      | Rahul    | DEPENDS_ON | DBOS     | 0.60       |
    When I extract the graph from "note-alpha"
    And the extractor returns the relations:
      | src_name | edge_type  | dst_name | confidence |
      | Rahul    | DEPENDS_ON | DBOS     | 0.90       |
    And I extract the graph from "note-beta"
    Then the graph has 2 nodes and 1 edges
    And the edge "Rahul" -"DEPENDS_ON"-> "DBOS" has weight 2
    And the edge "Rahul" -"DEPENDS_ON"-> "DBOS" has confidence 0.90
    And the edge "Rahul" -"DEPENDS_ON"-> "DBOS" cites both artifacts

  @doc-9 @idempotency
  Scenario: An edge is directional, so the reverse assertion is a different edge
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul depends on DBOS, and DBOS depends on Rahul.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
      | DBOS  | Tool      | 0.90       |
    And the extractor returns the relations:
      | src_name | edge_type  | dst_name | confidence |
      | Rahul    | DEPENDS_ON | DBOS     | 0.87       |
      | DBOS     | DEPENDS_ON | Rahul    | 0.87       |
    When I extract the graph from "note-alpha"
    Then the graph has 2 nodes and 2 edges
