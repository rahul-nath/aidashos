# Traces the live failure of 2026-08-04: milestone 0's dispatch intent was
# PAUSED, the milestone waited the full 1,800 seconds anyway, and then reported
# `dispatch_wait_elapsed` - "the agent did not answer" - about work the ledger
# knew had stopped on purpose 29 minutes earlier.
#
# Three separate holes produced that one line:
#   1. PAUSED and CHECKPOINT_REVIEW were not terminal, and there was no third
#      answer, so a waiter read them as "still running";
#   2. no status write except DONE and FAILED sent a notification, so nothing
#      shortened the wait;
#   3. the wait ran before the row was read, so even a settled intent whose
#      notification had already been consumed slept out its whole bound.
#
# The governing rule: a waiter must be able to tell "not finished yet" from
# "finished waiting for you".

Feature: When a milestone may stop waiting on its dispatch intent

  @settlement @exhaustiveness
  Scenario Outline: Every dispatch status has one of three answers
    When a dispatch intent is "<status>"
    Then a waiter is told "<progress>"

    Examples:
      | status            | progress |
      | PENDING           | ACTIVE   |
      | CLAIMED           | ACTIVE   |
      | IN_PROGRESS       | ACTIVE   |
      | CHECKPOINT_REVIEW | PARKED   |
      | PAUSED            | PARKED   |
      | DONE              | SETTLED  |
      | FAILED            | SETTLED  |
      | CANCELED          | SETTLED  |
      | SUPERSEDED        | SETTLED  |

  @settlement @pause @failure
  Scenario: A paused intent blocks the milestone by name, not by timeout
    Given a milestone waiting on a dispatch intent
    When the intent pauses at a checkpoint
    Then the milestone is blocked with failure code "dispatch_paused"
    And the failure names the checkpoint
    And the milestone did not wait out its bound

  @settlement @lost-notification
  Scenario: An already-settled intent costs no wait at all
    Given a milestone waiting on a dispatch intent
    When the intent settles before the wait begins
    Then the milestone reads the outcome without waiting
    # The live shape: a restart mid-flight leaves a `recv` whose notification was
    # already consumed. Waiting first meant sleeping the whole bound for a message
    # that no longer existed, with the answer sitting in a row nobody read.

  @settlement @timeout @failure
  Scenario: An intent still running when the clock runs out is a real timeout
    Given a milestone waiting on a dispatch intent
    When the wait expires while the intent is still claimed
    Then the milestone is blocked with failure code "dispatch_wait_elapsed"

  @settlement @notification
  Scenario Outline: Every transition a waiter can be parked on wakes it
    Given a dispatch intent with a milestone waiting on it
    When the intent becomes "<status>"
    Then the waiting milestone is notified

    Examples:
      | status     |
      | PAUSED     |
      | CANCELED   |
      | SUPERSEDED |
      | DONE       |
