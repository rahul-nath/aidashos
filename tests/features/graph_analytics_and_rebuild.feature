# Traces knowledge_graph_layer_gawd_doc_v1.md
#   analytics        §1.3 step 10
#   retention/replay §5, §9
#   rebuild SLO      §1.5 (graph rebuildable from artifacts, 100%)
#   verification     §11 (rebuild round-trip)

Feature: Graph analytics and rebuild

  The graph is a derived index. It holds nothing that is not re-derivable from
  the immutable entity_graph.v1 artifacts, which is what makes rebuild the
  canonical replay and makes every resolution decision reversible.

  Background:
    Given the ontology declares the entity types:
      | type    | description                          |
      | Person  | A named individual                   |
      | Project | A named initiative or body of work   |
      | Tool    | A software tool, library, or service |
    And the ontology declares the relation types:
      | type        | description                |
      | DEPENDS_ON  | Source requires the target |
      | AUTHORED_BY | Target created the source  |

  @doc-1.3 @analytics @happy-path
  Scenario: Analytics writes pagerank, degree, and community onto every node
    Given the graph holds:
      | src            | edge_type   | dst            |
      | Rahul          | AUTHORED_BY | local agent OS |
      | local agent OS | DEPENDS_ON  | DBOS           |
    When I run graph analytics
    Then the workflow completes with status "COMPLETED"
    And every node has a pagerank score
    And every node has a degree
    And every node has a community id
    And a "graph_metrics" artifact is persisted

  @doc-1.3 @analytics
  Scenario: Degree counts edges in both directions
    Given the graph holds:
      | src            | edge_type   | dst            |
      | Rahul          | AUTHORED_BY | local agent OS |
      | local agent OS | DEPENDS_ON  | DBOS           |
    When I run graph analytics
    Then the node "local agent OS" has degree 2
    And the node "Rahul" has degree 1

  @doc-1.3 @analytics
  Scenario: Disconnected clusters land in different communities
    Given the graph holds:
      | src   | edge_type  | dst   |
      | a     | DEPENDS_ON | b     |
      | c     | DEPENDS_ON | d     |
    When I run graph analytics
    Then the nodes "a" and "b" share a community
    And the nodes "a" and "c" do not share a community

  @doc-1.3 @analytics
  Scenario: Analytics over an empty graph is a clean no-op
    Given the graph is empty
    When I run graph analytics
    Then the workflow completes with status "COMPLETED"
    And a "graph_metrics" artifact is persisted

  @doc-9 @rebuild @slo-rebuildable
  Scenario: Rebuild re-derives an identical graph from the artifacts
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
    And I snapshot the graph
    And I rebuild the graph
    Then the graph matches the snapshot
    And the extractor was called 1 times

  @doc-9 @rebuild
  Scenario: Rebuild drops graph state that no artifact justifies
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul writes notes.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
    When I extract the graph from "note-alpha"
    And the graph gains an orphan node "Phantom" that no artifact asserts
    And I rebuild the graph
    Then the graph does not contain a node named "Phantom"
    And the graph contains a "Person" node named "Rahul"

  @doc-8 @rebuild
  Scenario: Raising the resolution threshold and rebuilding undoes an over-merge
    Given the ontology sets "resolution_threshold" to 0.60
    And an embeddable artifact "note-alpha" with the text:
      """
      Alex the reviewer and Alex the author.
      """
    And "Alex R" has embedding similarity 0.70 to "Alex A"
    And the extractor returns the entities:
      | name   | node_type | confidence |
      | Alex A | Person    | 0.95       |
      | Alex R | Person    | 0.95       |
    When I extract the graph from "note-alpha"
    Then the graph has 1 nodes and 0 edges
    When the ontology sets "resolution_threshold" to 0.90
    And I rebuild the graph
    Then the graph has 2 nodes and 0 edges

  @doc-5 @rebuild
  Scenario: Rebuild is deterministic across repeated runs
    Given an embeddable artifact "note-alpha" with the text:
      """
      Rahul depends on DBOS.
      """
    And the extractor returns the entities:
      | name  | node_type | confidence |
      | Rahul | Person    | 0.95       |
      | DBOS  | Tool      | 0.90       |
    When I extract the graph from "note-alpha"
    And I rebuild the graph
    And I snapshot the graph
    And I rebuild the graph
    Then the graph matches the snapshot
