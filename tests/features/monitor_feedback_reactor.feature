Feature: Monitor feedback reactor closes Monitor back to Plan
  The coordination ledger already records failed milestones and exhausted
  retries durably, and then waits for an operator to happen to look. The
  reactor turns those facts into proposed diagnosis work under budgets the
  operator owns, and never claims, merges, deploys, or approves anything.

  Background:
    Given a disposable coordination ledger
    And the starter feedback rule catalog

  Scenario: A failed milestone proposes exactly one diagnosis task
    Given a saga "pest_site_factory" with a milestone that failed with "PROCESS_FAILED"
    When the reactor runs one cycle
    Then exactly 1 advisory dispatch intent is PENDING
    And the proposed intent carries the feedback source for that milestone
    And the proposed intent names the failing evidence row in its prompt
    And exactly 1 feedback event is recorded with decision "PROPOSED"

  Scenario: An unchanged fact is not re-evaluated at all
    Given a saga "pest_site_factory" with a milestone that failed with "PROCESS_FAILED"
    And the reactor has run one cycle
    When the reactor runs one cycle
    Then exactly 1 advisory dispatch intent is PENDING
    And exactly 1 feedback event is recorded with decision "PROPOSED"
    And exactly 2 feedback events are recorded

  Scenario: A retry of the same condition reuses one fingerprint
    Given a saga "pest_site_factory" with a milestone that failed with "PROCESS_FAILED"
    And the reactor has run one cycle
    When the milestone fails again with "PROCESS_FAILED"
    And the reactor runs one cycle
    Then exactly 1 advisory dispatch intent is PENDING
    And exactly 1 feedback event is recorded with decision "DEDUPLICATED"

  Scenario: A different failure on the same milestone is a different condition
    Given a saga "pest_site_factory" with a milestone that failed with "PROCESS_FAILED"
    And the reactor has run one cycle
    When the milestone fails again with "ARGUMENT_LIST_TOO_LONG"
    And the reactor runs one cycle
    Then exactly 2 advisory dispatch intents are PENDING

  Scenario: The reactor never reacts to its own reaction
    Given a catalog that proposes advisory work for dispatch intent failures
    And a saga "pest_site_factory" with a milestone that failed with "PROCESS_FAILED"
    And the reactor has run one cycle
    When the proposed intent itself fails with "PROCESS_FAILED"
    And the reactor runs one cycle
    Then exactly 1 advisory dispatch intent exists in total
    And exactly 1 feedback event is recorded with decision "SUPPRESSED_LINEAGE"

  Scenario: A rule at its daily cap escalates instead of proposing
    Given a saga "pest_site_factory" with 6 milestones that failed with "PROCESS_FAILED"
    When the reactor runs one cycle
    Then exactly 4 advisory dispatch intents are PENDING
    And exactly 4 feedback events are recorded with decision "PROPOSED"
    And exactly 2 feedback events are recorded with decision "SUPPRESSED_BUDGET"

  Scenario: A project with no advisory rule still reaches the digest
    Given a saga "some_other_project" with a milestone that failed with "PROCESS_FAILED"
    When the reactor runs one cycle
    Then exactly 0 advisory dispatch intents are PENDING
    And exactly 2 feedback events are recorded with decision "ESCALATED_DIGEST"

  Scenario: A dry run decides everything and writes nothing
    Given a saga "pest_site_factory" with a milestone that failed with "PROCESS_FAILED"
    When the reactor runs one dry-run cycle
    Then exactly 0 advisory dispatch intents are PENDING
    And exactly 0 feedback events are recorded
    And the report counted 1 decision of "PROPOSED"

  Scenario: A crash before commit re-proposes rather than dropping the signal
    Given a saga "pest_site_factory" with a milestone that failed with "PROCESS_FAILED"
    And the reactor crashed after submitting its intent but before committing
    When the reactor runs one cycle
    Then exactly 1 advisory dispatch intent is PENDING
    And exactly 1 feedback event is recorded with decision "DEDUPLICATED"
