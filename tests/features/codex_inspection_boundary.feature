@native_codex_srt
Feature: Native Codex inspection has one AiDashOS-owned boundary
  A reviewer may read its assigned repository but cannot acquire command,
  write, network, or credential authority through the native tool transport.

  Background:
    Given the installed native Codex and pinned SRT profile
    And an assigned repository and synthetic host credentials

  Scenario: Native reviewer tools cannot expand inspection authority
    When the native inspection worker reads the repository and attempts forbidden effects
    Then the assigned repository text is returned
    And repository writes and process execution are denied without changing files
    And a forbidden host file is not returned

  Scenario: The operating-system boundary independently denies network and credentials
    When trusted diagnostic probes run inside the production SRT profile
    Then a working localhost TCP canary receives no worker connection
    And synthetic credentials are absent from worker environment and unreadable on disk
    And synthetic credential values do not appear in captured diagnostics
    And the operating-system repository write is denied and leaves its bytes unchanged

  Scenario: The prepared profile has no hidden broader grant
    When the production launch profile is prepared and inspected
    Then its effective authority is exactly read-only repository inspection
    And no permission bypass or local-network exception is enabled

  Scenario Outline: Native review completion owns all descendants
    When a real model-free review ends by <ending>
    Then all observed model-client and tool-worker descendants have exited
    And only successful completion emits a completed review

    Examples:
      | ending                      |
      | success                     |
      | worker failure              |
      | timeout                     |
      | cancellation                |
      | worker failure after report |
      | client failure after report |
