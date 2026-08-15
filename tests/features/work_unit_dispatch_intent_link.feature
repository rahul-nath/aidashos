# Traces the live failure of 2026-08-04: the API reported no dispatch intent for
# milestone 0 while `work_unit_events` sequence 24 named it. `DispatchIntentCreated`
# appended the event and updated nothing, so `milestone_executions.dispatch_intent_id`
# was NULL for every WorkUnit the live path ever produced.
#
# The blast radius is not cosmetic. `cancellation.run_cancellation_cascade`
# derives BOTH the set of intents to refuse AND the set of agent execution
# leases to stop from that column, so a cancellation stopped DBOS workflows and
# left the agent process running - verbatim the failure cancellation.py says it
# was written to fix.
#
# The governing rule: an event is history, a column is state, and a reader
# consulting state must not be told "no" by a row nobody wrote.

Feature: The link between a milestone and the agent work it asked for

  Background:
    Given a started WorkUnit whose first milestone is running

  @dispatch-link @happy-path
  Scenario: Creating an intent names it on the milestone attempt
    When the milestone creates dispatch intent "intent-alpha"
    Then the milestone attempt names "intent-alpha"
    And the event log also names "intent-alpha"

  @dispatch-link @idempotency
  Scenario: Replaying the same creation writes nothing twice
    When the milestone creates dispatch intent "intent-alpha"
    And the same creation is replayed
    Then the milestone attempt names "intent-alpha"
    And exactly one dispatch intent event was recorded

  @dispatch-link @retry
  Scenario: A retry's intent supersedes the one before it
    When the milestone creates dispatch intent "intent-alpha"
    And a second attempt creates dispatch intent "intent-beta"
    Then the milestone attempt names "intent-beta"
    # The intent's own idempotency key includes the attempt, so a second
    # creation is a genuinely new intent. The column names the live one.

  @dispatch-link @cancellation
  Scenario: Cancellation can reach the agent the intent started
    When the milestone creates dispatch intent "intent-alpha"
    Then cancelling the WorkUnit asks to stop "intent-alpha"

  @dispatch-link @failure
  Scenario: A concurrent write refuses rather than silently losing the link
    When the milestone attempt is modified underneath the write
    Then recording the intent creation fails loudly
