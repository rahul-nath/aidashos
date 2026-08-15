# Traces knowledge_graph_layer_gawd_doc_v1.md
#   happy path      §1.3 steps 1-9
#   inputs & bounds §2
#   units of work   §3
#   lifecycle       §4
#   bad outcomes    §8 (graph pollution)

Feature: Extracting an entity graph from one artifact

  One source artifact is the unit of work: the grain of retry, of idempotency,
  and of observability. Extraction turns an artifact's text into an immutable
  entity_graph.v1 artifact, then merges that artifact into the derived graph.
  If extraction of artifact A fails, B is unaffected.

  Background:
    Given the ontology declares the entity types:
      | type    | description                          |
      | Person  | A named individual                   |
      | Project | A named initiative or body of work   |
      | Concept | An idea, technique, or topic         |
      | Tool    | A software tool, library, or service |
    And the ontology declares the relation types:
      | type        | description                |
      | RELATES_TO  | General association        |
      | DEPENDS_ON  | Source requires the target |
      | AUTHORED_BY | Target created the source  |
      | ABOUT       | Source is primarily about the target |

  @doc-1.3 @happy-path
  Scenario: A note becomes nodes, edges, and mentions
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul is building the local agent OS. It depends on DBOS for durability.
      """
    And the extractor returns the entities:
      | name           | node_type | confidence |
      | Rahul          | Person    | 0.95       |
      | local agent OS | Project   | 0.90       |
      | DBOS           | Tool      | 0.88       |
    And the extractor returns the relations:
      | src_name       | edge_type   | dst_name | confidence |
      | local agent OS | AUTHORED_BY | Rahul    | 0.91       |
      | local agent OS | DEPENDS_ON  | DBOS     | 0.87       |
    When I extract the graph from "note-alpha"
    Then the workflow completes with status "COMPLETED"
    And the workflow stage is "COMPLETED"
    And an "entity_graph" artifact is persisted for "note-alpha"
    And the graph contains a "Person" node named "Rahul"
    And the graph contains a "Project" node named "local agent OS"
    And the graph contains a "Tool" node named "DBOS"
    And the graph has 3 nodes and 2 edges
    And the node "Rahul" has 1 mention of "note-alpha"

  @doc-1.3 @happy-path
  Scenario: The persisted artifact records what produced it
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul writes notes.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
    When I extract the graph from "note-alpha"
    Then the "entity_graph" artifact for "note-alpha" records the ontology version
    And the "entity_graph" artifact for "note-alpha" records the extractor model id
    And the "entity_graph" artifact for "note-alpha" names "note-alpha" as its source

  @doc-1.3 @lifecycle
  Scenario: Extraction walks the documented stages in order
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul writes notes.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
    When I extract the graph from "note-alpha"
    Then the workflow passed through the stages:
      | stage               |
      | VALIDATED           |
      | PROCESSING          |
      | ARTIFACT_PERSISTED  |
      | EGRESS_PENDING      |
      | COMPLETED           |

  @doc-2 @bounds
  Scenario: Only embeddable-text artifacts carry extractable entities
    Given a "browser_screenshot" artifact "shot-1"
    When I extract the graph from "shot-1"
    Then the workflow completes with status "COMPLETED"
    And no "entity_graph" artifact is persisted
    And the graph has 0 nodes and 0 edges

  @doc-2 @bounds @doc-8
  Scenario: Entities of a type outside the closed ontology are dropped and counted
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul went to Lisbon.
      """
    And the extractor returns the entities:
      | name   | node_type | confidence |
      | Rahul  | Person    | 0.95       |
      | Lisbon | Place     | 0.90       |
    When I extract the graph from "note-alpha"
    Then the workflow completes with status "COMPLETED"
    And the graph contains a "Person" node named "Rahul"
    And the graph does not contain a node named "Lisbon"
    And 1 entity was dropped as an unknown type

  @doc-2 @bounds
  Scenario: Relations of a type outside the closed ontology are dropped and counted
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul supervises the local agent OS.
      """
    And the extractor returns the entities:
      | name           | node_type | confidence |
      | Rahul          | Person    | 0.95       |
      | local agent OS | Project   | 0.90       |
    And the extractor returns the relations:
      | src_name | edge_type  | dst_name       | confidence |
      | Rahul    | SUPERVISES | local agent OS | 0.90       |
    When I extract the graph from "note-alpha"
    Then the workflow completes with status "COMPLETED"
    And the graph has 2 nodes and 0 edges
    And 1 relation was dropped as an unknown type

  @doc-2 @bounds
  Scenario: A relation naming an entity that was never extracted is dropped
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul depends on something unnamed.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
    And the extractor returns the relations:
      | src_name | edge_type  | dst_name | confidence |
      | Rahul    | DEPENDS_ON | Ghost    | 0.90       |
    When I extract the graph from "note-alpha"
    Then the workflow completes with status "COMPLETED"
    And the graph has 1 nodes and 0 edges
    And 1 relation was dropped as dangling

  @doc-2 @bounds @doc-8
  Scenario: A runaway model is capped at max_entities_per_artifact
    Given the ontology sets "max_entities_per_artifact" to 5
    And an embeddable artifact "note-alpha" with the text:
      """
      A rambling note that names many things.
      """
    And the extractor returns 12 distinct "Concept" entities
    When I extract the graph from "note-alpha"
    Then the workflow completes with status "COMPLETED"
    And the graph has 5 nodes and 0 edges
    And the extraction is flagged truncated
    And the "entity_graph" artifact for "note-alpha" is flagged truncated

  @doc-2 @bounds
  Scenario: Text longer than max_extraction_chars is windowed and the results unioned
    Given the ontology sets "max_extraction_chars" to 40
    And an embeddable artifact "long-note" with 130 characters of text
    When I extract the graph from "long-note"
    Then the extractor was called 4 times
    And the workflow completes with status "COMPLETED"

  @doc-2 @bounds
  Scenario: A batch larger than max_batch_artifacts is rejected before any work starts
    Given the ontology sets "max_batch_artifacts" to 2
    And 3 embeddable artifacts
    When I extract the graph from the whole batch
    Then the batch is rejected with an error naming the limit
    And the extractor was called 0 times
    And the graph has 0 nodes and 0 edges

  @doc-8 @quarantine
  Scenario: Low-confidence entities are written but quarantined for review
    Given the ontology sets "review_threshold" to 0.55
    And an embeddable artifact "note-alpha" with the text:
      """
      Something about stuff, maybe.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
      | Stuff | Concept   | 0.20       |
    When I extract the graph from "note-alpha"
    Then the workflow completes with status "COMPLETED"
    And the node "Stuff" is flagged for review
    And the node "Rahul" is not flagged for review

  @doc-8 @quarantine
  Scenario: Low-confidence relations are written but quarantined for review
    Given the ontology sets "review_threshold" to 0.55
    And an embeddable artifact "note-alpha" with the text:
      """
      Rahul might depend on DBOS.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
      | DBOS  | Tool      | 0.90       |
    And the extractor returns the relations:
      | src_name | edge_type  | dst_name | confidence |
      | Rahul    | DEPENDS_ON | DBOS     | 0.30       |
    When I extract the graph from "note-alpha"
    Then the edge "Rahul" -"DEPENDS_ON"-> "DBOS" is flagged for review
