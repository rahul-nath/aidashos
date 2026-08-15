---
name: handoff
description: Create durable, candid project handoff packets for agent-to-agent or session-to-session transfer. Use when work is pausing, context is compacting, another agent will continue, the user asks for a handoff, or the current agent needs to persist exact project state, validation evidence, blockers, and next steps into the coordination ledger or docs/handoffs.
---

# Handoff

Use this skill to leave a durable packet that lets the next agent resume without rereading the full chat. The handoff must be precise enough to act on and candid enough to prevent false completion.

## Workflow

1. Inspect current state before writing:
   - `pwd`
   - `git status --short`
   - `git branch --show-current`
   - relevant `git diff --stat` or focused diffs
   - latest validation commands and results
   - running processes or local services only when they affect resumption
2. Separate verified facts from assumptions. Do not say something is done unless a command, artifact, or live run proved it.
3. Write the handoff packet using the required sections below.
4. Persist it to the coordination ledger as canonical durable truth. Also write a readable mirror under `docs/handoffs/` when the packet is more than a short note or the user asked for a file.
5. In the final response, give the ledger scope/id or file path and call out any unverified or blocked items.

## Required Packet

Use these sections in this order:

```markdown
# Handoff: <project/task name>

## Current Goal
One or two sentences stating the actual objective and current status.

## Location
- repo: <absolute path>
- branch: <branch>
- related ledger/session ids: <ids or "unknown">

## Completed And Verified
- <thing completed> - proof: `<command>` -> <result>

## Changed Files
- `<path>` - <what changed and why>

## Validation
- `<command>` -> <exact result>
- Not run: <command> - <reason>

## Runtime State
- <service/process/model state that matters for resumption>

## Open Gaps
- <gap/blocker/risk, with precise status>

## Next Step
Start with: `<exact command or file to open>`
Then do: <one concrete next action>

## Do Not Re-Do
- <work already proven or decision already made>
```

## Style Rules

- Be candid. Say "not done", "not verified", or "blocked" when true.
- Use precise verbs: implemented, wired, validated, failed, skipped, blocked, observed.
- Include exact commands, paths, branch names, ids, ports, and model names when they matter.
- Mention uncommitted changes and whether they were yours, user changes, or unknown.
- Keep summaries dense. The next agent needs state, not a story.
- Do not bury the next step; make it directly executable.

## Persistence

Ledger is canonical. Prefer an `append_note` with a scope such as `handoff`, `handoff-final`, or `handoff-<slug>`:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python agent_coordination_mcp.py \
  --root /Users/rahul/.local-agent/coordination/local_first_agent_os \
  append_note \
  --session <session-id-if-known> \
  handoff-<slug> \
  '<one-line pointer to the handoff packet or compact summary>'
```

When the handoff is longer than a short ledger note, create a mirror file:

```text
docs/handoffs/YYYY-MM-DD-<slug>.md
```

Then append a ledger note pointing to that file. If no active ledger session is known, still write the docs mirror and say the ledger session was unknown.

## Completion Bar

The handoff is complete only when a next agent can answer these without reading the prior chat:

- What is the goal?
- What is already proven?
- What files changed?
- What command should I run first?
- What should I avoid redoing?
