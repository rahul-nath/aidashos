# Execution ordinals identify runs. Typed retry policies count only failures in
# which the milestone's work was judged, and an operator can override refusal.

Feature: How many times a blocked milestone may be tried again

  Background:
    Given a WorkUnit whose first milestone permits 5 charged failures

  @retry-budget @happy-path
  Scenario: A failed attempt inside the budget is retried
    Given the milestone is blocked on execution 1 after a correctable failure
    When the WorkUnit is resumed
    Then the milestone is ready on execution 2
    And nothing is reported as exhausted

  @retry-budget @failure
  Scenario: The last permitted attempt is the last one
    Given the milestone is blocked on execution 5 after a correctable failure
    When the WorkUnit is resumed
    Then the milestone is still blocked on execution 5
    And the resume reports it as exhausted
    And an operator override decision is waiting
    # Blocked, not failed. FAILED is terminal for a milestone, so failing it here
    # would make the override this same refusal offers unusable.

  @retry-budget @no-fault
  Scenario: Waiting for a person is not a spent attempt
    Given the milestone is blocked on execution 5 waiting for an operator decision
    When the WorkUnit is resumed
    Then the milestone is ready on execution 6
    # `review.operator` permits a single attempt, so counting its wait for a
    # human as a spent try would fail every approval gate the moment it asked.

  @retry-budget @override
  Scenario: An operator can decide the budget should not stop this one
    Given the milestone is blocked on execution 5 after a correctable failure
    And the WorkUnit was resumed and refused
    When the operator approves the override
    And the WorkUnit is resumed again
    Then the milestone is ready on execution 6

  @retry-budget @override
  Scenario: A denied override leaves the budget standing
    Given the milestone is blocked on execution 5 after a correctable failure
    And the WorkUnit was resumed and refused
    When the operator denies the override
    And the WorkUnit is resumed again
    Then the milestone is still blocked on execution 5

  @retry-budget @idempotency
  Scenario: Resuming an exhausted WorkUnit repeatedly does not buy attempts
    Given the milestone is blocked on execution 5 after a correctable failure
    When the WorkUnit is resumed 3 times
    Then the milestone is still blocked on execution 5
