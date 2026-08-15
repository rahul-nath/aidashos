# `execution_recovery` records the halt a dead execution never wrote, and it only
# ever ran when an operator resumed. Its docstring says so: "a reaper would have
# to answer the same question with nobody watching, and its wrong answers would
# be invisible." That objection is about evidence, and this reconciler answers it
# rather than dodging it.
#
# The design correction that matters: a crash writes no halt and therefore no
# failure code, so a reconciler CANNOT filter on
# `failure_code == execution_died_without_recording_a_halt`. That predicate finds
# exactly the WorkUnits an operator has already repaired, and none that crashed.
#
# The second correction: `execution_epoch` is not a crash-retry counter. It
# counts WORK_UNIT_BLOCKED and WORK_UNIT_WAITING_FOR_OPERATOR - every halt
# however caused - so a WorkUnit that asks three approval questions would exhaust
# its allowance for surviving crashes.
#
# The governing rule: never recover on an inference, only on an answer from the
# system that owns the fact.

Feature: Recovering an execution that died with nobody watching

  @crash-recovery @happy-path
  Scenario: A dead execution is repaired and resumed
    Given a running WorkUnit whose execution DBOS reports as dead
    When the reconciler sweeps
    Then the WorkUnit is recovered
    And it is resumed
    And an automatic crash recovery is recorded

  @crash-recovery @liveness
  Scenario Outline: Only a confirmed death is recovered
    Given a running WorkUnit whose execution DBOS reports as "<liveness>"
    When the reconciler sweeps
    Then the WorkUnit is left alone
    And nothing is resumed

    Examples:
      | liveness   |
      | LIVE       |
      | ABSENT     |
      | NO_RUNTIME |

  @crash-recovery @candidates
  Scenario: A WorkUnit that already recorded how it stopped is not a candidate
    Given a blocked WorkUnit
    When the reconciler sweeps
    Then nothing is inspected
    # There is no missing halt to write. Filtering on the dead-execution failure
    # code would have found this one and only this one.

  @crash-recovery @budget
  Scenario: A WorkUnit that keeps dying stops being restarted
    Given a running WorkUnit that has been recovered automatically 3 times
    When the reconciler sweeps
    Then the budget is reported as exhausted
    And nothing is resumed

  @crash-recovery @budget
  Scenario: Ordinary halts do not spend the automatic recovery budget
    Given a WorkUnit that has halted 5 times for operator decisions
    Then its automatic recovery count is 0
    # `execution_epoch` would say 5. The two counters answer different questions.

  @crash-recovery @idempotency
  Scenario: Two reconcilers recovering the same crash spend one budget entry
    Given a running WorkUnit whose execution DBOS reports as dead
    When two reconcilers sweep at once
    Then exactly one automatic crash recovery is recorded

  @crash-recovery @failure
  Scenario: A resume that could not be delivered is not reported as repaired work
    Given a running WorkUnit whose execution DBOS reports as dead
    And no durable runtime can take the continuation
    When the reconciler sweeps
    Then the WorkUnit is recovered
    But it is not reported as resumed
