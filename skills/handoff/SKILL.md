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

## Additional Ritual For A Long Session (operator instruction, 2026-08-16)

When the operator asks for a handoff at the end of a long chat, the packet above is the minimum and these are also required. Re-read the whole conversation before writing; a handoff assembled from memory of a long session will be wrong in the specific ways that matter.

1. **Lead with the command reference.** Operator commands get buried under diagnosis in a long chat. Restate the working `agent-ledger` and `scripts/` commands the next agent needs, including the ones whose output is misleading and what the misleading part means.
2. **What this chat did**, as verified outcomes rather than a narrative of attempts.
3. **Mistakes made, named plainly, with the rule that prevents each.** This section is not optional and not softened. A handoff that hides a mistake spends the next agent's time rediscovering it.
4. **Guards to harden.** For each mistake, say what would have caught it and whether that guard now exists. Note any check that was green while the thing it guards was broken, because that is a defective guard rather than a passing one.
5. **An ordered TODO**, most valuable first, each item actionable without rereading the chat.
6. **Design documents ready to run**, with milestone counts and compile status, and which are reserved or already running.
7. **Suggestions made but never spec'd**, so ideas raised in conversation are not lost when the context is.
8. **Update `README.md`** so newly created, completed, or restructured design documents sit in their proper section, then run `make design-status-check`.

Answer any operator question about publishing or syncing in the packet itself, with the distinction between "safe to sync" and "safe to make public" kept explicit.

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
