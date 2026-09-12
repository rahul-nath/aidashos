Feature: Unavailable review infrastructure is not implementation feedback

  Scenario Outline: Failed reviewer readiness prevents implementation work
    Given a local reviewer fixture fails at "<failure_point>"
    When the production executor checks reviewer readiness
    Then the run is blocked with REVIEW_UNAVAILABLE
    And no implementer or revision is started

    Examples:
      | failure_point |
      | containment   |
      | startup       |
      | tool-evidence |

  Scenario Outline: A late review infrastructure failure cannot request a code revision
    Given a local reviewer fixture fails at "<failure_point>"
    When the prepared review runner encounters that failure
    Then its report says CANNOT_REVIEW and parses as UNAVAILABLE
    When an implemented change receives that failed review report
    Then the review artifact is unavailable and never request_changes
    And the original host failure is retained without an implementation revision

    Examples:
      | failure_point |
      | containment   |
      | startup       |
      | tool-evidence |
