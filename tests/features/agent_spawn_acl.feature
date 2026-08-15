# The compiled plan already computes what each milestone's agent may do. Every
# ExecutorKind declares a `permitted_tools` tuple of Capability, the compiler
# copies it into the milestone's ToolPolicy, and it is hashed into the immutable
# plan. It then reached the agent's prompt as the line `Permitted tools: ...`
# and was dropped.
#
# The spawn decision was made somewhere else, from a boolean:
#
#   is_review = judgment.name == "reviewer" or "review" in task_name.lower()
#
# and every task that boolean called false was launched with
# `--dangerously-skip-permissions` or
# `--dangerously-bypass-approvals-and-sandbox`. A planning agent whose
# declaration permits only read_repository and invoke_model got an unsandboxed
# shell and write access to the checkout.
#
# The governing rule, from the compiled ACL document: a bypass flag is emitted
# if and only if the capability set holds both write_repository and run_command.

Feature: What a spawned agent process is allowed to do

  @spawn-acl @exhaustiveness
  Scenario Outline: The posture is the capability set, not the task's name
    When the capability set is "<capabilities>"
    Then the spawn posture is "<posture>"

    Examples:
      | capabilities                            | posture                   |
      | read_repository                         | read_only_inspection      |
      | read_repository,invoke_model            | read_only_inspection      |
      | read_repository,write_artifact          | read_only_inspection      |
      | read_repository,run_command             | supervised_commands       |
      | read_repository,write_repository        | read_only_inspection      |
      | read_repository,write_repository,run_command | unattended_implementation |

  @spawn-acl @bypass
  Scenario Outline: Only an unattended implementer gets a bypass flag
    Given a "<harness>" agent
    When the spawn posture is "<posture>"
    Then the command <bypass> a permission bypass

    Examples:
      | harness | posture                   | bypass         |
      | codex   | read_only_inspection      | does not carry |
      | codex   | supervised_commands       | does not carry |
      | codex   | unattended_implementation | carries        |
      | claude  | read_only_inspection      | does not carry |
      | claude  | supervised_commands       | does not carry |
      | claude  | unattended_implementation | carries        |

  @spawn-acl @narrowing
  Scenario: A reviewer does not inherit the implementer's ceiling
    Given a dispatch intent whose ceiling permits writing and running
    When a review task is spawned under it
    Then the spawn posture is "read_only_inspection"
    # One milestone's intent fans out into an implementer, a reviewer, and a
    # junior. Handing the ceiling to every task gives the reviewer write access.

  @spawn-acl @narrowing
  Scenario: A task cannot exceed the ceiling its milestone declared
    Given a dispatch intent whose ceiling permits only reading
    When an implementation task is spawned under it
    Then the spawn posture is "read_only_inspection"

  @spawn-acl @failure
  Scenario: An intent that declares nothing gets the narrowest authority
    Given a dispatch intent with no capability set at all
    When an implementation task is spawned under it
    Then the spawn posture is "read_only_inspection"
    # The opposite of what the boolean did. Absence used to mean "not a review",
    # which meant the sandbox came off.

  @spawn-acl @failure
  Scenario: An unreadable capability set is not a permissive one
    Given a dispatch intent whose capability set is malformed
    When an implementation task is spawned under it
    Then the spawn posture is "read_only_inspection"

  @spawn-acl @provenance
  Scenario: The run artifact records the authority the process was given
    Given a dispatch intent whose ceiling permits writing and running
    When an implementation task is spawned under it
    Then the recorded posture matches the command that was built
