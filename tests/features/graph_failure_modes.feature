# Traces knowledge_graph_layer_gawd_doc_v1.md
#   failure semantics §4
#   dependency map    §12
#   log guarantees    §1.3
#
# The governing rule from §1.3: no silent failures. Malformed model output is
# always persisted as an artifact before any terminal transition.

Feature: How graph extraction fails

  Background:
    Given the ontology declares the entity types:
      | type    | description                          |
      | Person  | A named individual                   |
      | Tool    | A software tool, library, or service |
      | Concept | An abstract idea or named document   |
    And the ontology declares the relation types:
      | type       | description                |
      | DEPENDS_ON | Source requires the target |

  @doc-4 @failure @retryable
  Scenario: A model timeout leaves the run retryable and writes nothing to the graph
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul depends on DBOS.
      """
    And the extractor times out
    When I extract the graph from "note-alpha"
    Then the workflow completes with status "FAILED_RETRYABLE"
    And the graph has 0 nodes and 0 edges
    And no "entity_graph" artifact is persisted

  @doc-4 @doc-12 @failure @retryable
  Scenario: An unloaded general model is a retryable failure, not a crash
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul depends on DBOS.
      """
    And the general model is not loaded
    When I extract the graph from "note-alpha"
    Then the workflow completes with status "FAILED_RETRYABLE"
    And the graph has 0 nodes and 0 edges

  @doc-4 @failure @repair
  Scenario: Malformed JSON is repaired once and the run succeeds
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul depends on DBOS.
      """
    And the extractor returns the raw output:
      """
      Sure! Here is the graph you asked for:
      ```json
      {"entities": [{"name": "Rahul", "node_type": "Person", "confidence": 0.9}],
       "relations": []}
      ```
      """
    When I extract the graph from "note-alpha"
    Then the workflow completes with status "COMPLETED"
    And the graph contains a "Person" node named "Rahul"

  @doc-4 @failure @permanent
  Scenario: Malformed JSON that survives the repair attempt is permanent, with the raw output kept
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul depends on DBOS.
      """
    And the extractor returns the raw output:
      """
      I would rather not produce JSON today.
      """
    When I extract the graph from "note-alpha"
    Then the workflow completes with status "FAILED_PERMANENT"
    And a "model_output" artifact is persisted for "note-alpha"
    And the graph has 0 nodes and 0 edges
    And the extractor was called 2 times

  @doc-3 @doc-4 @failure @isolation
  Scenario: One artifact's failure does not stop the rest of the batch
    Given an embeddable artifact "note-good" with the text:
      """
      Rahul depends on DBOS.
      """
    And an embeddable artifact "note-bad" whose extraction times out
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
    When I extract the graph from the whole batch
    Then the run for "note-good" completes with status "COMPLETED"
    And the run for "note-bad" completes with status "FAILED_RETRYABLE"
    And the graph contains a "Person" node named "Rahul"

  @doc-4 @failure @idempotent-recovery
  Scenario: A merge that fails partway can be safely re-run
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul depends on DBOS.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
      | DBOS  | Tool      | 0.90       |
    And the graph store fails after writing the first node
    When I extract the graph from "note-alpha"
    Then the workflow completes with status "FAILED_RETRYABLE"
    And the graph has 1 nodes and 0 edges
    When the graph store recovers
    And I extract the graph from "note-alpha" again
    Then the workflow completes with status "COMPLETED"
    And the graph has 2 nodes and 0 edges
    And the node "Rahul" has 1 mention of "note-alpha"

  @doc-12 @degraded
  Scenario: Without the embedder, resolution degrades to exact matching and the run still completes
    Given an embeddable artifact "note-alpha" with the text:
      """
      The GAWD doc explains it.
      """
    And the graph already contains a "Concept" node named "GAWD doc"
    And the embedder is not loaded
    And the extractor returns the entities:
      | name         | node_type | confidence |
      | the gawd doc | Concept   | 0.90       |
    When I extract the graph from "note-alpha"
    Then the workflow completes with status "COMPLETED"
    And the graph has 1 nodes and 0 edges
    And the run is flagged as resolution-degraded

  @doc-1.3 @logging
  Scenario: Every write records whether it created or merged, and by which path
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul depends on DBOS.
      """
    And the graph already contains a "Person" node named "Rahul"
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
      | DBOS  | Tool      | 0.90       |
    When I extract the graph from "note-alpha"
    Then the node "Rahul" was merged by the "exact" path
    And the node "DBOS" was created

  @doc-13 @security
  Scenario: Snippets and entity names never reach the INFO log
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul's private diagnosis is recorded here.
      """
    And the extractor returns the entities:
      | name                    | node_type | confidence |
      | Rahul private diagnosis | Concept   | 0.90       |
    When I extract the graph from "note-alpha"
    Then no INFO log line contains "private diagnosis"

  @doc-13 @security
  Scenario: Medical artifacts are skipped unless the workspace policy allows it
    Given a medical embeddable artifact "med-note" with the text:
      """
      Patient notes.
      """
    And the medical workspace does not allow embedding its outputs
    When I extract the graph from "med-note"
    Then the workflow completes with status "COMPLETED"
    And no "entity_graph" artifact is persisted
    And the extractor was called 0 times
