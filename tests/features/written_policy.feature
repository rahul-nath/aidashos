# The operator's own statement of who may do what, and the seam where it meets
# the runtime.
#
# `POLICIES.md` is written by a person in the vocabulary a person decides in -
# `deploy`, `merge_to_main`, `code_worktree_write` - and enforced by a supervisor
# that only knows capabilities. `work_units/permissions.py` owns the translation
# between the two, so the word an operator approves in an intake document means
# exactly the same thing when they write it here.
#
# The translation is not one-to-one, and that is where the edge cases live. One
# action can mean several capabilities, so two policy lines can contradict each
# other without sharing a single word.
#
# The governing rule: a report that cannot say no always says yes. A `Never:`
# line a runtime grant could lift would make this file advisory, and an advisory
# policy is a comment.

Feature: The written policy document

  @policy @vocabulary
  Scenario Outline: A line may be written in either vocabulary and means one thing
    Given a policy that denies "<written>" to claude
    When the policy is asked whether claude may "<capability>"
    Then the policy says "<verdict>"

    Examples: the operator's vocabulary
      | written             | capability             | verdict |
      | deploy              | publish_deployment     | no      |
      | merge_to_main       | merge_to_main          | no      |
      | code_worktree_write | write_repository       | no      |
      | code_worktree_write | run_command            | yes     |

    Examples: the runtime's vocabulary
      | written             | capability             | verdict |
      | publish_deployment  | publish_deployment     | no      |
      | run_command         | run_command            | no      |
      | run_command         | write_repository       | yes     |

    Examples: one action, several capabilities
      | written             | capability             | verdict |
      | dependency_install  | run_command            | no      |
      | dependency_install  | network_access         | no      |
      | dependency_install  | write_repository       | yes     |

  @policy @vocabulary @failure
  Scenario: Two lines contradict each other without sharing a word
    Given a policy that allows "test_command_execution" and denies "dependency_install" to claude
    Then the document refuses to compile
    And the complaint names "test_command_execution"
    And the complaint names "dependency_install"
    And the complaint names "run_command"
    # Both expand to `run_command`. Resolving it to the denial would be safe and
    # still wrong: the operator would be left with a May line that does nothing
    # and no sign that it does nothing. Only they know which one they meant.

  @policy @failure
  Scenario: A rule that governs nothing is refused rather than ignored
    Given a policy that denies "prepare_isolated_worktrees" to claude
    Then the document refuses to compile
    And the complaint names "prepare_isolated_worktrees"
    # It is real authoring vocabulary that needs no runtime capability. Written
    # here it would read as protection and be none.

  @policy @failure
  Scenario: A name from neither vocabulary is refused
    Given a policy that denies "become_root" to claude
    Then the document refuses to compile
    And the complaint names "become_root"

  @policy @authority
  Scenario: The written policy outranks a runtime grant
    Given nothing has been revoked for this pow-wow
    When the gate is asked whether claude may "publish_deployment"
    Then the gate says "denied"
    And the denial names "POLICIES.md"
    # This is the property that makes writing the file worth the trouble. The
    # plan is the grant everywhere else in the ACL; here it is not enough.

  @policy @authority
  Scenario Outline: Every operator-gated action is denied to an agent invented later
    When the policy is asked whether "some-agent-invented-later" may "<capability>"
    Then the policy says "no"

    Examples:
      | capability                  |
      | publish_deployment          |
      | merge_to_main               |
      | spend_money                 |
      | external_communications     |
      | access_credentials          |
      | destructive_file_operations |
    # The default section is what makes this true. Without one, a principal with
    # no section of its own inherits the absence of a denial rather than the
    # denial, and every new agent starts unrestricted.
