# Continuous integration

The public repository's [CI workflow](../.github/workflows/ci.yml) checks pull requests, pushes to `main`, merge-queue candidates and manual runs.
The `CI gate` check passes only when every required group succeeds.
Failure, cancellation or a skipped group prevents that gate from passing.

| Group | What it checks |
| --- | --- |
| Quality and documentation | Ruff lint and formatting, Pyright, generated OpenAPI/configuration documents, README links and CI helper tests |
| PostgreSQL test suite | The public Python suite and verifier helper protocol tests against disposable PostgreSQL 16 with pgvector |
| Frontend (web) | Locked dependencies, ESLint, generated API types and the dashboard production build |
| Frontend (landing_page_website) | Locked dependencies, TypeScript, browser and server builds, and page prerendering |
| macOS containment | Selected real Seatbelt, child-process and native broker controls on macOS, without provider accounts |

## Execution environment

Python and Node versions come from `.python-version` and `.node-version`.
The uv version comes from `scripts/toolchain-pins.env`.
Python dependencies use `uv sync --locked --dev`; frontend dependencies use their committed npm lockfiles.
The dashboard API generator explicitly requests `openapi-typescript@7.13.0`, as declared in its `package.json` script.
Third-party Actions are pinned to commit SHAs.

Dependency acquisition happens on GitHub-hosted runners.
The workflows do not install models, invoke paid providers, use Neon or receive operator credentials.
Their token has read-only repository contents permission, and checkout does not persist Git credentials.
Pull requests use `pull_request`, not `pull_request_target`.

The full suite runs in four disjoint file partitions on macOS, the supported runtime platform.
Each partition starts and stops its own temporary PostgreSQL 16 cluster with commit-pinned pgvector because the public local-resource contract explicitly requires major version 16.
Python Playwright installs Chromium on those hosted runners for the real browser acceptance tests.
The separate containment group uses PostgreSQL 18; that narrower result does not claim a migration of the versioned local-resource contract.
These jobs supply only `LOCAL_AGENT_TEST_DATABASE_URL`; neither points tests at an operational database.
The Node preparation script copies the runner's installed distribution into a job-owned NVM directory required by runtime discovery.

## Test scope and results

The public suite reports its skip reasons and uploads JUnit results.
Some public tests require macOS, an explicitly enabled browser/provider installation, or private documents that are not distributed.
Those skips remain visible rather than being presented as executed tests.
The selected macOS containment tests must collect and run with zero skips.

This macOS group does not claim qualification of an installed privileged UID verifier or authenticated Codex/Claude integrations.
Those host installation checks remain in the [verifier setup guide](../scripts/verifier_uid/README.md).
The native CI group instead exercises the provider-free containment contracts with actual processes and kernel enforcement.

Documentation-only tests run with `--noconftest` because the repository-wide fixture provisions PostgreSQL for every test.
This exception is restricted to the file-only documentation and CI-helper tests.
The backend and native jobs use the ordinary database fixtures.

## Merge gate

Require the GitHub Actions `CI gate` status on the default branch and require it to be current with the target branch.
Keep the existing pull-request and review rules.
A separate required-check ruleset can enforce CI without granting the existing owner bypass permission to skip those checks.
The README's live badge follows the workflow's `main` push results; its linked run identifies the commit, logs and artifacts.
Historical release validation remains in the corresponding public pull request.
