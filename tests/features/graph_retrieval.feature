# Traces knowledge_graph_layer_gawd_doc_v1.md
#   GraphRAG retrieval §1.3 steps 11-13
#   retrieval bounds   §7 (configs/ontology.toml [retrieval])
#   opt-in tradeoff    §Intentional Tradeoffs 3
#   verification       §11 (GraphRAG 2-hop fixture)

Feature: Graph-augmented retrieval

  Vector search finds entry points; graph traversal expands them. The bridge
  from vector hits to graph nodes is graph_node_mentions, a cheap relational
  join. Plain vector retrieval stays untouched: GraphRAG is an opt-in path.

  Background:
    Given the ontology declares the entity types:
      | type    | description                          |
      | Person  | A named individual                   |
      | Project | A named initiative or body of work   |
      | Tool    | A software tool, library, or service |
    And the ontology declares the relation types:
      | type       | description                |
      | DEPENDS_ON | Source requires the target |
      | AUTHORED_BY | Target created the source |

  @doc-1.3 @graphrag @happy-path
  Scenario: A vector hit is expanded into its neighborhood
    Given the graph holds:
      | src   | edge_type   | dst            |
      | Rahul | AUTHORED_BY | local agent OS |
    And the chunk "chunk-1" mentions the node "Rahul"
    And a vector search for "who writes the notes" returns "chunk-1"
    When I ask the graph "who writes the notes"
    Then the neighborhood seeds are "Rahul"
    And the neighborhood contains the node "local agent OS"
    And the neighborhood contains the edge "Rahul" -"AUTHORED_BY"-> "local agent OS"

  @doc-11 @graphrag
  Scenario: A two-hop link the vector store alone would miss
    Given the graph holds:
      | src            | edge_type   | dst            |
      | Rahul          | AUTHORED_BY | local agent OS |
      | local agent OS | DEPENDS_ON  | DBOS           |
    And the chunk "chunk-1" mentions the node "Rahul"
    And a vector search for "what does Rahul's work rest on" returns "chunk-1"
    When I ask the graph "what does Rahul's work rest on"
    Then the neighborhood contains the node "DBOS" at 2 hops

  @doc-7 @bounds
  Scenario: Traversal stops at max_hops
    Given the ontology sets "max_hops" to 1
    And the graph holds:
      | src            | edge_type   | dst            |
      | Rahul          | AUTHORED_BY | local agent OS |
      | local agent OS | DEPENDS_ON  | DBOS           |
    And the chunk "chunk-1" mentions the node "Rahul"
    And a vector search for "what does Rahul's work rest on" returns "chunk-1"
    When I ask the graph "what does Rahul's work rest on"
    Then the neighborhood contains the node "local agent OS"
    And the neighborhood does not contain the node "DBOS"

  @doc-7 @bounds
  Scenario: Traversal stops at max_neighbors
    Given the ontology sets "max_neighbors" to 3
    And the node "hub" has 10 neighbors
    And the chunk "chunk-1" mentions the node "hub"
    And a vector search for "the hub" returns "chunk-1"
    When I ask the graph "the hub"
    Then the neighborhood contains 3 nodes besides the seeds

  @doc-1.3 @graphrag
  Scenario: The neighborhood carries the mention snippets that justify it
    Given the graph holds:
      | src   | edge_type   | dst            |
      | Rahul | AUTHORED_BY | local agent OS |
    And the chunk "chunk-1" mentions the node "Rahul" with the snippet "Rahul is building it"
    And a vector search for "who writes the notes" returns "chunk-1"
    When I ask the graph "who writes the notes"
    Then the neighborhood carries the snippet "Rahul is building it" for "Rahul"

  @doc-1.3 @graphrag
  Scenario: An empty graph degrades to the vector hits alone
    Given the graph is empty
    And a vector search for "anything" returns "chunk-1"
    When I ask the graph "anything"
    Then the neighborhood seeds are ""
    And the neighborhood contains 0 nodes besides the seeds

  @doc-1.3 @graphrag
  Scenario: A vector hit whose chunk touches no node contributes no seed
    Given the graph holds:
      | src   | edge_type   | dst            |
      | Rahul | AUTHORED_BY | local agent OS |
    And the chunk "chunk-2" mentions no node
    And a vector search for "unrelated" returns "chunk-2"
    When I ask the graph "unrelated"
    Then the neighborhood seeds are ""

  @tradeoff-3 @opt-in
  Scenario: Plain retrieval is unchanged by the presence of a graph
    Given the graph holds:
      | src   | edge_type   | dst            |
      | Rahul | AUTHORED_BY | local agent OS |
    And the chunk "chunk-1" mentions the node "Rahul"
    And a vector search for "who writes the notes" returns "chunk-1"
    When I run plain retrieval for "who writes the notes"
    Then the result is exactly the vector hits
