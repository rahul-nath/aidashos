# `all(capture.exit_code == 0 for capture in verification_captures)` is `True` on
# an empty tuple. Three sites in `pow_wow/executor.py` gated a checkpoint commit
# on exactly that, so a project declaring `verification_commands = []` produced a
# checkpoint, a completed task, and a `CODE_MERGE` approval having verified
# nothing - and every reader downstream that trusts "verification passed" was
# trusting it.
#
# The defect is not the missing conditional. One boolean was carrying two
# questions: did anything run, and did what ran succeed. A tuple has no room to
# say "nothing was ever going to run here", so the answer is a sum with a member
# for each, and exactly one of them certifies.
#
# The governing rule: a report that cannot say no always says yes.

Feature: Whether a run may leave a checkpoint behind

  @verification @exhaustiveness
  Scenario Outline: Every combination of declared commands and captures has one answer
    Given "<declared>" verification commands are declared
    And "<captures>" of them ran with "<results>"
    Then the verification outcome is "<outcome>"
    And a checkpoint is "<permitted>"

    Examples:
      | declared | captures | results | outcome      | permitted |
      | 0        | 0        | none    | not_declared | refused   |
      | 2        | 2        | 0,0     | passed       | permitted |
      | 2        | 2        | 0,1     | failed       | refused   |
      | 2        | 0        | none    | incomplete   | refused   |
      | 2        | 1        | 0       | incomplete   | refused   |

  @verification @failure
  Scenario: A run that verified nothing is refused a checkpoint and says why
    Given a code task whose target project declares no verification commands
    When the task runs and its agent command succeeds
    Then no checkpoint is committed
    And the task is reported failed
    And the failure names the project and how to fix it

  @verification @happy-path
  Scenario: A run whose declared verification passes is certified as before
    Given a code task whose target project declares a passing verification command
    When the task runs and its agent command succeeds
    Then a checkpoint is committed
    And the task is reported completed

  @verification @failure
  Scenario: A declared verification that fails still blocks the checkpoint
    Given a code task whose target project declares a failing verification command
    When the task runs and its agent command succeeds
    Then no checkpoint is committed
    And the task is reported failed

  @verification @registry
  Scenario: A writable project cannot declare an empty verification list
    Given a linked project registry entry that is writable with no verification commands
    Then loading the registry fails
    And the complaint names the project and both remedies

  @verification @registry
  Scenario: A read-only project may declare none, having no code runs to certify
    Given a linked project registry entry that is read-only with no verification commands
    Then loading the registry succeeds
    # `dispatcher_runner` refuses code intents for a read-only project before an
    # executor is reached, so there is nothing for it to verify.
