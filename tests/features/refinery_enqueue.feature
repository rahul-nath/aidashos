# Where an approved agent branch enters the refinery, and every reason it may not.
#
# Enqueue happens on resolution to APPROVED and not on submission, because a
# request queued before a human agreed to it would let the refinery merge
# something nobody approved. That is invariant 1 of
# docs/refinery_integration_queue_design.md, and also `AGENT_BRANCH_AUTO_MERGE =
# False` set to True by a different route.
#
# The refusals are the content. Each one names a precondition the rest of the
# design assumes and never re-checks, so an approval that fails one fails it here
# rather than three milestones later with a stack half built. The one worth
# reading twice is GATE_NOT_DECLARED: `all(... for ... in ())` is True, so a
# project with no verification commands would hand every stack a green gate it
# never ran, which is the same vacuous truth that already produced one class of
# false certification in `pow_wow/executor.py`.
#
# INTEGRATED_BRANCH_MISSING is the one that is about the operator's clock rather
# than their diff. A declared branch the repository does not have would be found
# by the loop, at whatever hour it next polled, and reported to nobody. Asking
# the question at approval time puts the answer in front of the person who just
# typed approve.
#
# The governing rule: an approval the queue cannot act on is not an approval.

Feature: Admitting an approved commit to the integration queue

  @refinery @exhaustiveness
  Scenario Outline: Every reason an approved CODE_MERGE is or is not landable
    Given a linked project that is "<project>"
    And a CODE_MERGE approval whose payload is "<payload>"
    When the refinery is asked to admit it
    Then the admission is "<outcome>"

    Examples:
      | project   | payload           | outcome                        |
      | writable  | complete          | admitted                       |
      | writable  | no_commit_sha     | MALFORMED_SUBJECT              |
      | writable  | no_provenance     | MALFORMED_SUBJECT              |
      | writable  | abbreviated_sha   | MALFORMED_SUBJECT              |
      | writable  | unknown_project   | PROJECT_NOT_LINKED             |
      | readonly  | complete          | PROJECT_IS_READ_ONLY           |
      | no_gate   | complete          | GATE_NOT_DECLARED              |
      | no_branch | complete          | INTEGRATED_BRANCH_MISSING      |
      | writable  | absent_commit     | COMMIT_NOT_IN_REPOSITORY       |
      | writable  | absent_base       | COMMIT_NOT_IN_REPOSITORY       |
      | writable  | commit_is_base    | COMMIT_IS_ITS_OWN_BASE         |
      | writable  | unrelated_commit  | COMMIT_NOT_DESCENDED_FROM_BASE |

  @refinery @provenance
  Scenario: An admitted request carries the approval that authorises it
    Given a linked project that is "writable"
    And a CODE_MERGE approval whose payload is "complete"
    When the refinery is asked to admit it
    Then the queued request names the approval, the intent, and the pow-wow it came from
    And the queued request lands the commit rather than the branch name

  @refinery @boundary
  Scenario: Approving a CODE_MERGE puts it in the queue
    Given a target repository with an approved agent branch
    When the operator resolves the CODE_MERGE approval to approved
    Then the approval is APPROVED and one request is queued for the project
    And the queued request binds the exact commit the approval named

  @refinery @idempotence
  Scenario: A replayed resolution yields one request
    Given a target repository with an approved agent branch
    When the operator resolves the CODE_MERGE approval to approved
    And the same commit is enqueued a second time
    Then the project has exactly one queued request

  @refinery @boundary
  Scenario: A refused enqueue refuses the resolution
    Given a target repository with an approved agent branch
    And the approval payload names a commit the repository does not have
    When the operator resolves the CODE_MERGE approval to approved
    Then the resolution is refused naming the missing commit
    And the approval is still PENDING and nothing is queued

  @refinery @boundary
  Scenario: Denying a CODE_MERGE queues nothing
    Given a target repository with an approved agent branch
    When the operator resolves the CODE_MERGE approval to denied
    Then the approval is DENIED and nothing is queued

  @refinery @boundary
  Scenario: An operator can read the queue they just put something in
    Given a target repository with an approved agent branch
    When the operator resolves the CODE_MERGE approval to approved
    Then listing the project's integration requests shows the queued commit
    And listing a state nothing is in shows nothing

  @refinery @scope
  Scenario: An approval that is not about code is not the queue's business
    Given a target repository with an approved agent branch
    When a PURCHASE approval for the same saga is resolved to approved
    Then the approval is APPROVED and nothing is queued
