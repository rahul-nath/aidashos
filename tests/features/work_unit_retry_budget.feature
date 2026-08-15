# The compiled plan carries a per-milestone attempt budget - three for most
# executor kinds, one for `review.operator`. Exactly one place consulted it, and
# that place ran while the WorkUnit was scheduling and only for milestones whose
# status was FAILED. BLOCKED is where a real executor failure lands, so the
# budget applied to the one status a failed run never occupies: a BLOCKED
# attempt 3 became attempt 4, and N resumes bought N attempts.
#
# What made it hard to simply tighten is that BLOCKED is reached four ways and
# only one of them spent a try. The milestone row kept `failure_code` (free text,
# written in four places) and threw away the `FailureClass` the scheduler already
# had, so the distinction was destroyed at the write. It is a datatype problem,
# and the fix is a column plus a typed decision, not a conditional.
#
# The governing rule: a bound nobody can exceed by asking again, and a refusal
# that names the way past it.

Feature: How many times a blocked milestone may be tried again

  Background:
    Given a WorkUnit whose first milestone permits 3 attempts

  @retry-budget @happy-path
  Scenario: A failed attempt inside the budget is retried
    Given the milestone is blocked on attempt 1 after a correctable failure
    When the WorkUnit is resumed
    Then the milestone is ready on attempt 2
    And nothing is reported as exhausted

  @retry-budget @failure
  Scenario: The last permitted attempt is the last one
    Given the milestone is blocked on attempt 3 after a correctable failure
    When the WorkUnit is resumed
    Then the milestone is still blocked on attempt 3
    And the resume reports it as exhausted
    And an operator override decision is waiting
    # Blocked, not failed. FAILED is terminal for a milestone, so failing it here
    # would make the override this same refusal offers unusable.

  @retry-budget @no-fault
  Scenario: Waiting for a person is not a spent attempt
    Given the milestone is blocked on attempt 3 waiting for an operator decision
    When the WorkUnit is resumed
    Then the milestone is ready on attempt 4
    # `review.operator` permits a single attempt, so counting its wait for a
    # human as a spent try would fail every approval gate the moment it asked.

  @retry-budget @override
  Scenario: An operator can decide the budget should not stop this one
    Given the milestone is blocked on attempt 3 after a correctable failure
    And the WorkUnit was resumed and refused
    When the operator approves the override
    And the WorkUnit is resumed again
    Then the milestone is ready on attempt 4

  @retry-budget @override
  Scenario: A denied override leaves the budget standing
    Given the milestone is blocked on attempt 3 after a correctable failure
    And the WorkUnit was resumed and refused
    When the operator denies the override
    And the WorkUnit is resumed again
    Then the milestone is still blocked on attempt 3

  @retry-budget @idempotency
  Scenario: Resuming an exhausted WorkUnit repeatedly does not buy attempts
    Given the milestone is blocked on attempt 3 after a correctable failure
    When the WorkUnit is resumed 3 times
    Then the milestone is still blocked on attempt 3
