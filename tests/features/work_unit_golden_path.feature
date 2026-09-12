# Recovery scenarios exercise durable ledger semantics.
# The composed happy path has one executable owner:
# test_the_golden_path_runs_through_the_resident_loops in test_work_unit_golden_path.py.
# make test-golden-path requires that real subprocess test rather than a no-op BDD alias.

Feature: A WorkUnit driven from a document to SUCCEEDED

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
