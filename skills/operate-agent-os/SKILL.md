---
name: operate-agent-os
description: Drive this local-first agent OS from your own AI tool (Claude Code, Codex, or any MCP client). Use when a user asks to check the system, boot it, attach over MCP, author or approve work, dispatch, or read the ledger from an interactive session.
---

# Operate the agent OS

This skill is for the operator's own AI tool, working interactively at the repo root.
Dispatched agents inside the system follow `skills/agent-startup/SKILL.md` instead; do not confuse the two lanes.

## Attach over MCP

The coordination ledger is an MCP server: `uv run agent-ledger serve` (stdio, run from the repo root, or pass `--root /path/to/repo` before `serve`).

- Claude Code: the repo ships `.mcp.json`, so opening a session at the repo root offers the `agent-os` server automatically.
- Codex: add to `~/.codex/config.toml`, with the absolute repo path:

```toml
[mcp_servers.agent-os]
command = "uv"
args = ["run", "agent-ledger", "--root", "/absolute/path/to/aidashos", "serve"]
```

- Anything else that speaks MCP over stdio: the same command.

The server needs the ledger's Postgres, which `./scripts/start-agent-runtime.sh` starts.
If tools fail with connection errors, start the runtime first.

One refusal reads like a connection error and is not one.
When this checkout's `SCHEMA_VERSION` is ahead of the database, every connection is refused and the message says `needs migration` and names the database.
That is a reachable ledger telling you the two of you disagree about its shape.
Run `agent-ledger migrate_coordination_schema` after checking that the database is the one you meant to change.
Connecting never migrates on its own, because a worktree that did exactly that took the shared ledger down twice on 2026-08-17.

## First moves in any session

1. `run_first_run_check` (MCP) or `./scripts/first-run-check.sh` (shell): is this machine ready, and if not, what exact command fixes it.
2. If the machine has never booted, run the boot sequence: `./scripts/boot/boot.sh`, stages documented in `scripts/boot/README.md`.
3. `describe_resident_loops`, `list_dispatch_intents`, `read_execution_ledger`: establish what is running and what has actually happened before asserting anything.
4. Execution history is a query, not an inference: if you did not read it from the ledger, do not claim it.

## Driving work

Work starts from a design document, not a prompt.

```bash
uv run pi /start /new-project      # author the document, file-first
uv run pi /approve-most-recent     # review plan, permissions, gates
uv run pi /dispatch                # claim one milestone and run it
uv run pi /ledger                  # inspect sagas, tasks, approvals
uv run agent-ledger compile_design_doc <path>   # compile a doc by path
```

The ledger MCP tools mirror `agent-ledger`'s subcommands, with one deliberate exception; prefer the tools for reads and structured writes, and the pi commands for the governed workflows.
`migrate_coordination_schema` is a shell verb only, on neither MCP server.
Reshaping a database several processes share is a decision an operator types, not a tool a model can reach for.

One difference worth knowing: run from a shell, the commands that print a WorkUnit or a plan also print the follow-up commands to stderr, with every id substituted and the unusable ones marked with the code they would fail on.
The MCP tools return the payload only.
When you are telling a human what to run next, either run the shell command and quote what it printed, or compose the argv from the payload yourself; do not tell them a verb is available without checking its preconditions against the same payload.

## Boundaries that are not yours to cross

- Approval gates (`CODE_MERGE`, `PURCHASE`, `EXTERNAL_COMMS`, `MODEL_ESCALATION`, `REVIEW_ESCALATION`) are resolved by the human operator.
  Surface pending requests; never submit the decision unless the operator explicitly tells you to in this session.
- The system never auto-merges, deploys, or spends; do not work around that with your own shell.
- Coordination flows through ledger rows and artifacts, not chat memory; if you coordinated something, record it.
- Validation is `uv run pytest`, `uv run ruff check`, `uv run pyright`, the same commands the seats run.

## References

- `docs/AGENT_MANUAL.md`: what the system is for and how you take a project from idea to merged milestone. Read this first if you have not driven the system before; this skill is the attach-and-command reference, that is the end-to-end one.
- `docs/onboarding/ONBOARDING.md`: the single-lane walkthrough this skill assumes.
- `docs/diagrams/aidashos-onboarding-dag.png`: the whole lane as a DAG.
- `README.md`: what the system is and what a fresh clone can do.
- `POLICIES.md`: the written policy the capability gate enforces at runtime.
