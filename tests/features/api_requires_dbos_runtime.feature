Feature: The API refuses to serve without the runtime it is configured for
  The API process delivers WorkUnits, resumes them, and recovers dead
  executions through the DBOS runtime its lifespan launches. A swallowed
  launch failure once left such a server answering requests with all three
  silently unavailable, which is a refusal presenting as a success.

  Scenario: the configured runtime fails to launch
    Given settings that require the durable runtime
    And a durable runtime whose launch raises
    When the API starts
    Then startup is refused with a message naming the durable runtime

  Scenario: the runtime is configured off
    Given settings that do not use the durable runtime
    When the API starts
    Then the API serves and health reports the runtime is not launched

  Scenario: the configured runtime launches
    Given settings that require the durable runtime
    And a durable runtime that launches cleanly
    When the API starts
    Then the API serves and health reports the runtime is launched
