# Two surfaces that computed a failure and then declined to report it.
#
# `emit_progress` builds an operator sentence and logged the literal
# `dispatch_progress` instead, so all eleven call sites produced log lines whose
# message was the same word. The actionable `401 Not logged in` from a failed
# junior task lived in a `PowWowTaskResult.risks` tuple nothing forwarded, and
# reached only `agent_execution_leases.result_json`.
#
# `drain_enqueue_outbox` caught a root-execution failure, bumped the outbox row's
# attempt counter, and `continue`d without appending anything, so its return
# value had no representation for "this row raised". Every consumer read that
# silence as an empty queue.
#
# The governing rule, from the handoff that named both: a report that cannot say
# no always says yes.

Feature: Failures that reach somebody

  @surfaced-failures @logging
  Scenario: The sentence a progress event computes reaches the log
    When a progress event says "the junior turn failed"
    Then the log carries "the junior turn failed"
    And the log message is still "dispatch_progress"

  @surfaced-failures @logging
  Scenario: A failed task's own reasons reach the log
    When a task finishes failed with the risk "401 Not logged in"
    Then the log carries "401 Not logged in"

  @surfaced-failures @failure
  Scenario: A progress field that would overwrite a log attribute is refused
    When a progress event names a field "module"
    Then it is refused

  @surfaced-failures @delivery
  Scenario: A root execution that raises is a failed delivery, not a silence
    Given an enqueued WorkUnit whose start raises
    When the outbox is drained
    Then the drain reports one failed delivery
    And the failure names why

  @surfaced-failures @delivery
  Scenario: A drainer whose every row crashes does not look like a quiet queue
    Given an enqueued WorkUnit whose start raises
    When the drainer polls 3 times
    Then it reports itself stalled
    And the stall names the rows that raised

  @surfaced-failures @delivery
  Scenario: A drain command whose rows raised exits non-zero
    Given an enqueued WorkUnit whose start raises
    When the drain command runs
    Then the command reports failure
