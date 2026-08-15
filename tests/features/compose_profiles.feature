# Which services make up a stack, and where that fact is written down.
#
# It used to be written down in every script that needed it. The eight-service
# telemetry list was byte-identical in start-docker-compose-infra.sh,
# stop-docker-compose-infra.sh, and start-local-observability.sh, and appears
# again in stop-agent-runtime.sh, alongside the compose file that actually
# defines the services. Adding one telemetry container was a five-file edit, and
# the compose file was the only one of the five that failed loudly when it was
# missed. The others failed by quietly starting or stopping the wrong set.
#
# `docs/compose_profiles_design.md` moves the fact into `docker-compose.yml`,
# next to the services, and leaves the scripts naming a stack.
#
# The load-bearing choice is that `postgres` carries a `core` profile rather than
# no profile at all. Compose selects a profile-less service on every
# profile-scoped command, so a profile-less postgres would be swept up by
# `stop-docker-compose-infra.sh observability`, which exists to stop the
# telemetry and spare the ledger the running agent is talking to.
#
# Nothing here starts Docker. The scenarios resolve Compose's own selection rule
# against the file, and read the argv the scripts hand to a recording stand-in
# for the `docker` binary, so the coverage holds with the daemon stopped.

Feature: Which services a named stack is

  @compose-profiles @single-source
  Scenario: A script names a stack rather than the services in it
    Given the compose file and the infrastructure scripts as they ship
    Then the compose infrastructure scripts name no service on a compose command line
    And every stack an infrastructure script asks for is declared in the compose file
    # "The compose infrastructure scripts" is exactly two files:
    # start-docker-compose-infra.sh and stop-docker-compose-infra.sh, the pair
    # the design doc maps to profile invocations. start-local-observability.sh
    # and stop-agent-runtime.sh still name services from their own lists; that
    # is outside this guarantee on purpose, and the copy tripwire at the bottom
    # is what keeps those lists honest until they are migrated.
    #
    # The second step is what stops the first from being satisfiable by a typo.
    # A profile nothing declares is not an error to Compose; it selects nothing
    # and the command succeeds, so the stack silently comes up empty.

  @compose-profiles @stack-membership
  Scenario Outline: Asking the start script for a stack brings up that stack
    When the start script is asked for "<stack>"
    Then the services brought up are "<services>"

    Examples:
      | stack                 | services                                                                       |
      | postgres              | postgres                                                                       |
      | observability-minimal | postgres, prometheus                                                           |
      | observability         | alloy, grafana, loki, minio, minio-init, postgres, prometheus, pyroscope, tempo |

  @compose-profiles @teardown-asymmetry
  Scenario: Stopping the telemetry leaves the ledger running
    When the stop script is asked for "observability"
    Then "postgres" is spared
    And the services taken down are "alloy, grafana, loki, minio, minio-init, prometheus, pyroscope, tempo"
    # The order is deliberate. Sparing Postgres is the reason this script has an
    # `observability` arm at all, so it is asserted ahead of the list and names
    # its own cause when somebody deletes postgres's `core` profile on the theory
    # that a profile-less service is simpler because it always starts.

  @compose-profiles @teardown-asymmetry
  Scenario: Stopping Postgres takes nothing else with it
    When the stop script is asked for "postgres"
    Then the services taken down are "postgres"

  @compose-profiles @stack-membership
  Scenario: No service joins a stack by having no opinion
    Given the compose file and the infrastructure scripts as they ship
    Then every service declares at least one stack
    # A profile-less service belongs to every profile-scoped selection at once,
    # which is a membership none of the tables above can express. The cost is
    # that a bare `docker compose up` now refuses instead of starting eleven
    # containers nobody asked for, and an explicit refusal is the better default.

  @compose-profiles @stack-membership
  Scenario: A command that asks for no stack selects nothing
    Given the compose file and the infrastructure scripts as they ship
    Then a compose command that activates no stack selects no services
    # The other half of the trade the scenario above makes. With every service
    # profiled, the profile-less invocation is empty by construction, and that
    # includes `docker compose down`: bare `down` tears down nothing and exits
    # 0. The whole-project teardown is `docker compose --profile "*" down`,
    # and the compose file says so where the profiles are declared.

  @compose-profiles @stack-membership
  Scenario Outline: A service nobody asks for by stack is in no stack
    Given the compose file and the infrastructure scripts as they ship
    Then "<service>" is in no stack an infrastructure script can ask for

    Examples:
      | service       |
      | app           |
      | postgres-test |
    # `app` is the only service with a `build:` stanza, so joining an
    # infrastructure stack would make asking for a database trigger an image
    # build. `postgres-test` is a fixture; the suite starts it by name, which
    # works whether or not its profile is active.

  @compose-profiles @stack-membership
  Scenario: The suite keeps its database because the autostart names it
    Given the compose file and the infrastructure scripts as they ship
    Then the suite's database autostart names "postgres-test" and asks for no stack
    And the smoke script starts "postgres" by name and asks for no stack
    # The design doc calls this the property that makes adopting profiles safe:
    # the suite's autostart path keeps working unchanged. The scenario above
    # asserts the hazard, that `postgres-test` is in no stack; this one asserts
    # the mitigation, that its callers start it by explicit name. Losing the
    # mitigation breaks every database test in the suite, so it must fail here
    # first and be redone on purpose.

  @compose-profiles @single-source
  Scenario Outline: A copy of a stack list still left in a script agrees with the compose file
    Given the compose file and the infrastructure scripts as they ship
    Then the "<array>" list in "<script>" is exactly the "<stacks>" stacks

    Examples:
      | script                       | array                 | stacks                |
      | start-local-observability.sh | OBSERVABILITY         | observability         |
      | start-local-observability.sh | MINIMAL_OBSERVABILITY | observability-minimal |
      | stop-agent-runtime.sh        | COMPOSE_SERVICES      | core, observability   |
    # A tripwire, not an endorsement. These three copies survive because
    # `docs/compose_profiles_design.md` maps only the two infrastructure scripts
    # to profile invocations; the `stop` and `logs` paths that use these were
    # left alone. Until they are gone, this is what makes forgetting one of them
    # loud rather than silent, which is the complaint the design doc opens with.
    #
    # `stop-agent-runtime.sh` is a fourth copy the design doc does not mention,
    # found by looking for `docker compose` rather than by reading the doc. It is
    # the reason to assert this rather than to trust the count of three.
