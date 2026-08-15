# The path nothing has ever driven end to end.
#
# `tests/conftest.py` pins LOCAL_AGENT_USE_DBOS=false before the package is
# imported, because `@dbos_step` and `@dbos_workflow` bind at import time. So the
# ordinary suite has never run a real DBOS workflow, and every green WorkUnit
# trace in it went through the simulated runtime and never submitted a dispatch
# intent at all. Every defect in the 2026-08-04 handoffs lived in the gap between
# those two facts.
#
# The full drive - DesignDoc -> compile -> start -> enqueue drainer -> DBOS ->
# resident dispatcher -> local junior delegate -> dispatch settlement -> artifacts
# -> operator decision -> SUCCEEDED - runs the two resident loops as real
# subprocesses against disposable databases, because that is the only shape where
# "production resident constructors" is literally true and the only one that
# exercises DBOS's cross-process notification path. It is gated on
# LOCAL_AGENT_RUN_POSTGRES_INTEGRATION=1.
#
# The edge cases below it are ledger semantics and run in the ordinary lane.

Feature: A WorkUnit driven from a document to SUCCEEDED

  @golden-path @integration @happy-path
  Scenario: The whole path, through the resident loops
    Given a disposable coordination ledger and DBOS system database
    And the golden path design doc is compiled and started
    When the enqueue drainer and the resident dispatcher are running
    Then the first milestone reaches a real dispatch intent
    And the local junior delegate answers it
    And the milestone records its artifact
    When the operator approves the review milestone
    Then the WorkUnit reaches SUCCEEDED

  @golden-path @lost-notification
  Scenario: A settlement whose notification was already consumed
    Given a milestone waiting on a dispatch intent that has already settled
    When the milestone waits
    Then it reads the outcome without waiting out its bound

  @golden-path @pause
  Scenario: A checkpoint pauses the intent the milestone is waiting on
    Given a milestone waiting on a dispatch intent
    When the intent pauses at a checkpoint
    Then the milestone is blocked with failure code "dispatch_paused"

  @golden-path @cancellation
  Scenario: Cancelling a WorkUnit stops the lease its intent started
    Given a running milestone whose dispatch intent has an open execution lease
    When the WorkUnit is cancelled
    Then the lease is asked to stop

  @golden-path @idempotency
  Scenario: Two reconcilers repairing one crash spend one budget entry
    Given a WorkUnit whose execution died
    When two crash reconcilers sweep
    Then exactly one automatic crash recovery is recorded

  @golden-path @budget
  Scenario: An exhausted attempt budget stops the resume
    Given a milestone blocked on its last permitted attempt
    When the WorkUnit is resumed
    Then the milestone is not made ready again
    And an operator override decision is waiting
