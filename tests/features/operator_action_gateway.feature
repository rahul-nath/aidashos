Feature: One canonical gateway for every operator action
  Operator surfaces must present and submit the same typed action instead of
  exposing the mutation primitives that implement it.

  Scenario Outline: Every operator action type crosses the same state-derived gateway
    Given a durable subject in the "<subject_state>" state
    When the CLI, React cockpit, MCP, Pi, HTTP, and notification adapters request its operator actions
    Then every adapter receives the same "<action_kind>" action projection
    And the projection contains "<primary_count>" primary advancing actions
    When the operator follows the projected "<decision>" through the gateway
    Then the gateway invokes "<command_count>" "<command_kind>" typed commands
    And no adapter invokes a mutation implementation directly
    And an action-bearing submission is idempotent when replayed
    And an action-bearing projection becomes stale after an authority-bearing state change
    And an action-bearing stale submission is refused before any durable mutation

    Examples:
      | subject_state                         | action_kind                 | primary_count | decision  | command_count | command_kind                 |
      | draft_gawd                            | approve_gawd                | 1             | approve   | 1             | approve_gawd_doc             |
      | pending_work_unit_decision            | resolve_work_unit_decision  | 1             | approve   | 1             | resolve_work_unit_decision   |
      | reviewed_merge_pending_approval       | approve_code_merge          | 1             | approve   | 1             | approve_code_merge           |
      | recoverable_blocked_work_unit         | resume_work_unit            | 1             | resume    | 1             | resume_work_unit             |
      | integrated_commit_awaiting_credit     | adopt_integrated_milestone  | 1             | adopt     | 1             | adopt_integrated_milestone   |
      | approved_integration_awaiting_drain   | trigger_integration         | 1             | integrate | 1             | trigger_integration          |
      | cancellable_work_unit                 | cancel_work_unit            | 0             | cancel    | 1             | cancel_work_unit             |
      | no_operator_work                      | no_advancing_action         | 0             | none      | 0             | none                         |
