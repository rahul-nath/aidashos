# aidashos

## Plans, not prompts.

When a coding task needs more than a chat, aidashos gives your AI tool a local path from design document to independently reviewed, approval-gated change.

[Quickstart](https://www.aidashos.com/quickstart/) · [Docs](https://www.aidashos.com/docs/) · [Website](https://www.aidashos.com/) · [Source](https://github.com/rahul-nath/aidashos)

- Compile design documents into immutable, hashed plans.
- Run local models and subscription-backed coding agents through one governed bench.
- Isolate implementation in Git worktrees and verify it with the project's own commands.
- Preserve milestones, attempts, artifacts, reviews, and decisions in a local Postgres ledger.
- Stop before merge, deploy, spend, or external communication for operator approval.

The default setup uses your logged-in Codex and Claude Code CLIs, so aidashos needs no per-token model API integration.
Provider subscription limits still apply.
Local models handle routine judgment, and frontier pairings can move between providers on a later attempt.
Automatic all-local senior and staff fallback is not shipped yet.

## Start

```bash
git clone https://github.com/rahul-nath/aidashos.git && cd aidashos && make
./scripts/boot/boot.sh
```

Or [hand the setup to your current coding agent](https://www.aidashos.com/quickstart/).

Claude Code discovers the local ledger from the checked-in `.mcp.json`.
Codex and other stdio MCP clients use the config in the [operator skill](skills/operate-agent-os/SKILL.md).

Teach your agent when to use it:

```text
When a task needs durable state, separate implementation and review, operator approvals, recovery, or evidence that must survive this session, route it through AiDashOS and follow <AIDASHOS_ROOT>/skills/operate-agent-os/SKILL.md.

Use a direct single pass for a bounded local change.
```

macOS is supported.
Linux is expected to work but is not exercised on a schedule.
Windows is not supported.

Public developer preview.
AGPL-3.0-or-later.
