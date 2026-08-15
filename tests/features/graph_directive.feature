# Traces knowledge_graph_layer_gawd_doc_v1.md
#   directive surface §7 (pi /graph subcommands)
#   operator recovery §8, §10 (runbook hooks)
#   security          §13 (workspace path bounds)

Feature: The pi /graph directive

  /graph is the operator's whole interface to the layer: build it, analyze it,
  query it, inspect it, and re-derive it. Every subcommand is parsed into a
  DirectiveSpec before any work is scheduled.

  @doc-7 @directive
  Scenario Outline: Each documented subcommand parses
    When I parse the directive "<directive>"
    Then the directive action is "graph"
    And the graph subcommand is "<subcommand>"

    Examples:
      | directive                     | subcommand |
      | /graph build                  | build      |
      | /graph analyze                | analyze    |
      | /graph get "who wrote this"   | get        |
      | /graph node "Rahul"           | node       |
      | /graph stats                  | stats      |
      | /graph review                 | review     |
      | /graph rebuild                | rebuild    |

  @doc-7 @directive
  Scenario: Bare /graph is rejected with the subcommands named
    When I parse the directive "/graph"
    Then parsing fails with an error naming the subcommands

  @doc-7 @directive
  Scenario: An unknown subcommand is rejected rather than guessed at
    When I parse the directive "/graph frobnicate"
    Then parsing fails with an error naming the subcommands

  @doc-7 @directive
  Scenario: build takes an optional path
    When I parse the directive "/graph build /tmp/notes"
    Then the graph subcommand is "build"
    And the directive path is "/tmp/notes"

  @doc-7 @directive
  Scenario: build with no path means every existing embeddable artifact
    When I parse the directive "/graph build"
    Then the graph subcommand is "build"
    And the directive has no path

  @doc-7 @directive
  Scenario Outline: The query subcommands require their argument
    When I parse the directive "<directive>"
    Then parsing fails with an error naming the missing argument

    Examples:
      | directive   |
      | /graph get  |
      | /graph node |

  @doc-13 @security
  Scenario: build refuses a path outside the workspace policy roots
    Given the workspace policy root is "/tmp/allowed"
    When I run "/graph build /etc"
    Then the run is refused with a path-policy error
    And the extractor was called 0 times

  @doc-8 @doc-10 @operator
  Scenario: review lists exactly what was quarantined
    Given the graph already contains a "Concept" node named "Stuff" flagged for review
    And the graph already contains a "Person" node named "Rahul"
    When I run "/graph review"
    Then the review output lists "Stuff"
    And the review output does not list "Rahul"

  @doc-10 @operator
  Scenario: stats reports counts and the highest-ranked nodes
    Given the graph holds:
      | src            | edge_type   | dst            |
      | Rahul          | AUTHORED_BY | local agent OS |
      | local agent OS | DEPENDS_ON  | DBOS           |
    And graph analytics has run
    When I run "/graph stats"
    Then the stats report 3 nodes and 2 edges
    And the stats name the top-ranked node

  @doc-10 @operator
  Scenario: stats on an empty graph says so rather than failing
    Given the graph is empty
    When I run "/graph stats"
    Then the stats report 0 nodes and 0 edges
