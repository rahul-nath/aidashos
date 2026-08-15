# Traces the live failure of 2026-08-04: a resident dispatcher claimed a junior
# dispatch intent, the bench staffed junior with harness "pi" and model "gemma4",
# and the executor launched
#
#   claude --print --output-format stream-json --verbose --model gemma4 <prompt>
#
# which returned `401 Not logged in` from an account that has no such model.
#
# Two independent defects produced one symptom, so both are covered here:
#   1. the runner was constructed with no delegate callback, so junior work was
#      never diverted to the local model at all;
#   2. the command builder branched on codex and treated every other harness as
#      claude, so the diversion failing was silent rather than loud.
#
# The governing rule: a harness that has no CLI must never be answered with
# somebody else's CLI.

Feature: Where a junior task actually runs

  Background:
    Given a bench that staffs junior with the local harness and model "gemma4"
    And a bench that staffs senior with claude and staff with codex

  @routing @local-harness @happy-path
  Scenario: A resident dispatcher runs junior work on the local model
    Given a dispatcher runner built the way the resident loop builds one
    When a junior task is routed
    Then the task runs on the local delegate
    And no frontier CLI is spawned

  @routing @local-harness @failure
  Scenario: A local-harness task with no delegate is refused, not rerouted
    Given an executor built with no delegate callback
    When a junior task is routed
    Then the task fails
    And the failure names the local harness it could not call
    And no frontier CLI is spawned

  @routing @exhaustiveness @failure
  Scenario: The local harness has no command line to build
    When the frontier command builder is asked for the local harness
    Then it is a type error, and at runtime the classification refuses it

  @routing @harness-not-tier
  Scenario: Staffing junior with claude routes junior work to claude
    Given a bench that staffs junior with claude instead
    When a junior task is routed
    Then a claude command is built
    # The delegate path used to be chosen by tier, so this operator got the local
    # model back no matter what they configured. The harness is what they changed.

  @routing @exhaustiveness
  Scenario Outline: Each frontier harness builds its own command line
    When the frontier command builder is asked for "<harness>" with review "<review>"
    Then the command starts with the "<harness>" binary
    And the command contains "<flag>"

    Examples:
      | harness | review | flag                                        |
      | codex   | yes    | read-only                                   |
      | codex   | no     | --dangerously-bypass-approvals-and-sandbox  |
      | claude  | yes    | --print                                     |
      | claude  | no     | --dangerously-skip-permissions              |

  @routing @fallback
  Scenario: The cross-provider fallback only ever names the other frontier
    When claude fails with a usage limit
    Then the alternate frontier selected is codex
    When codex fails with a usage limit
    Then the alternate frontier selected is claude

  @routing @provenance
  Scenario: A resident delegate opens the workflow its model calls belong to
    Given a dispatcher runner built the way the resident loop builds one
    When a junior task is routed
    Then a workflow run exists for the pow-wow the task belonged to
    And the model call is recorded against that workflow
    # `model_invocations.workflow_id` is NOT NULL REFERENCES workflow_runs, so a
    # delegate with no registered workflow cannot record that it ran at all.
