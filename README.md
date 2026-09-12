# aidashos

## Make Plans, Not Prompts

Give your coding agent a plan, then let aidashos track the work from the first change to the approved merge.
It runs on your computer and saves progress, test results, reviews and decisions in a local database.
You can see what happened, what is blocked and what needs your approval.

[Setup guide](https://aidashos.com/quickstart/) · [Docs](https://aidashos.com/docs/) · [Website](https://aidashos.com/) · [Report a bug](https://github.com/rahul-nath/aidashos/issues)

## The golden path: from an idea to a finished change

The **golden path** is the normal workflow when everything is ready.
Say you want to add a search box to an app:

1. **Describe the result.** Ask your agent to write a plan: what the search box should do, what it must leave alone and how to test it.
   Aidashos checks that the plan has the information it needs and saves a fixed version before work starts.
2. **Approve the plan.** Review the steps and the access the agent needs.
   The saved plan defines the work you are approving.
3. **Let an agent make the change.** It works in a separate Git working copy, called a *worktree*, so it can edit and test the code without changing your main branch.
4. **Check the work.** Aidashos runs the project's test commands, and a separate AI session reviews the change.
   It saves the results alongside the exact code that was checked.
5. **Approve the merge.** You inspect the proposed change and its evidence.
   Once you approve it and the required checks pass, aidashos adds it to the main branch.
6. **Get the result.** Your agent reports what changed and where it landed.
   The saved history lets you trace the result back to its plan, tests, review and approval.

A plan can contain several smaller steps, called *milestones*.
Aidashos tracks each one so a longer job does not depend on a single chat remembering everything.
Your agent can help you write the structured design document, called a **CGD**.

## When something stops

If a test fails, a model is unavailable or an approval is missing, the next step is to inspect that blocker.
Your agent can read the saved status and help you fix it, retry it or decide what to do next.
Recovery can require your input.

The history lives in PostgreSQL, a database on your computer, so ending a chat or restarting the runtime does not erase the recorded work.
Your computer still needs to be awake, with the required services available, for agents to keep working.
Keep backups of that database and your repositories.

## Get started

**macOS is the supported platform.**
The contained verifier uses macOS-specific security features; this release does not promise a working Linux or Windows setup.

The easiest way to begin is to [give the setup prompts to your local coding agent](https://aidashos.com/quickstart/).
If you prefer the terminal:

```bash
git clone https://github.com/rahul-nath/aidashos.git && cd aidashos && make
./scripts/boot/boot.sh
```

Setup installs tools and a local database, then walks you through local-model downloads and provider sign-ins.
The macOS verifier also needs administrator-approved installation and checks on your machine.
Follow the [setup instructions](docs/onboarding/ONBOARDING.md) and [verifier setup](scripts/verifier_uid/README.md) before starting work.

Once setup is complete, give your agent this instruction:

```text
Use aidashos for this task.
Read skills/operate-agent-os/SKILL.md in my aidashos checkout.
Check that the system is ready, help me write a plan with clear tests,
and show me the plan and required permissions before starting work.
```

[The operator guide](skills/operate-agent-os/SKILL.md) explains how to connect your agent and inspect or resume work.
Claude Code can discover the connection through the included `.mcp.json` file; the guide includes setup for Codex and other compatible clients.

## Which models does it use?

In the default setup, that is Codex implementing and Codex reviewing in separate sessions.
Claude Code is available as a fallback.
These use your existing provider sign-ins and subscription limits.
Local models handle smaller decisions.
Running the whole implementation-and-review workflow on local models is not available yet.

## Status

Aidashos is a public developer preview.
Expect rough edges, read proposed changes before approving them and [report problems](https://github.com/rahul-nath/aidashos/issues).

Licensed under [AGPL-3.0-or-later](LICENSE).
