# The seam between the two halves of the ACL.
#
# The compiled plan says what an executor kind may do, `SpawnAuthority` turns
# that into sandbox flags, and the plan hash is immutable - all decided at
# compile time and none of it revocable. `capability_gate` is the other half: a
# live check an operator can act on. They had never met, because the principal
# that joins them - `AgentCaller` - was constructed nowhere in the codebase.
#
# It was worse than not connected. `check_capability` passed `capability.value`
# in as a *tool name*, and `check_tool_call` matches tool names against
# hardcoded sets like `send_email` and `git_merge`. No Capability value appears
# in any of them, so the function always returned granted, whatever the ledger
# said. It looked like enforcement because it called the policy engine.
#
# The governing rule: a check that cannot fail is not a check.

Feature: Whether a spawned agent may use what its plan gave it

  @acl @exhaustiveness
  Scenario Outline: A gated capability can be refused; an ungated one has nothing to refuse
    Given the capability "<capability>" is revoked for this pow-wow
    When the gate is asked about "<capability>"
    Then the gate says "<verdict>"

    Examples:
      | capability         | verdict |
      | write_repository   | denied  |
      | run_command        | denied  |
      | publish_deployment | denied  |
      | read_repository    | granted |
      | invoke_model       | granted |
      | ask_operator       | granted |

  @acl @scope
  Scenario: A revocation in another pow-wow does not reach this one
    Given the capability "write_repository" is revoked for a different pow-wow
    When the gate is asked about "write_repository"
    Then the gate says "granted"
    # The scope is the point. Unscoped, the ledger answers about every pow-wow
    # this agent name ever appeared in.

  @acl @happy-path
  Scenario: The plan is the grant, so an unrevoked capability passes
    Given nothing has been revoked
    When the gate is asked about "write_repository"
    Then the gate says "granted"
    # Requiring a second, hand-typed grant for every implementation milestone
    # would teach an operator to grant reflexively, which costs the word its
    # meaning and buys nothing the plan did not already say.

  @acl @spawn @failure
  Scenario: A revoked capability stops the next spawn
    Given a code task whose plan permits writing and running
    And "write_repository" is revoked for its pow-wow
    When the task is routed
    Then the task fails without spawning a process
    And the failure names the capability and how to restore it

  @acl @spawn
  Scenario: Revocation does not reach a task that never needed the capability
    Given an advisory task whose plan permits only reading
    And "write_repository" is revoked for its pow-wow
    When the task is routed
    Then the task is not refused by the gate
