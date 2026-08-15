---
name: agent-startup
description: Orient agents before working in the local_first_agent_os repo. Use when a new Codex/Claude/local agent starts work, resumes after a handoff, touches dispatcher/reactor, pow-wow, decomposition, staffing, ledger, runtime scripts, Pi skills, or needs the system architecture and workflow before making changes.
---

# Agent Startup

Use this skill before non-trivial repo work. The goal is to prevent agents from
rediscovering the architecture, bypassing durable state, or treating subagents as
shared chat/process state.

## First Checks

Run these from the repo root:

```bash
pwd
git status --short --branch
git worktree list
git log --oneline --decorate -8
```

If local services matter, also check:

```bash
curl -fsS http://127.0.0.1:8766/health
curl -fsS http://127.0.0.1:8765/health
ps -axo pid,ppid,command | rg "pi-daemon|session-daemon|llama-server|whisper-server"
```

Do not assume resident daemons are running the current checkout. If runtime
freshness matters, restart with:

```bash
./scripts/stop-agent-runtime.sh
./scripts/start-agent-runtime.sh
```

## Architecture Map

Keep these boundaries straight:

- `agent_coordination_mcp.py` is the durable coordination ledger CLI/MCP.
- `dispatcher.py` claims queued dispatch intents and records terminal outcomes.
- `dispatcher_runner.py` adapts a claimed intent into saga, pow-wow, task, and artifact ledger rows.
- `decomposition.py` turns one intent into a validated `DecompositionPlan`: a scoped `MiniGawdDoc` plus a `PowWowTaskSpec` DAG.
- `new_project_intake.py` owns the file-first sparse GAWD draft, finalized draft, permission envelope, and finalization task contract.
- `pow_wow/executor.py` schedules and runs tasks by `blocked_by`, tier capacity, dispatch kind, and worktree group.
- `staffing.py` is the only source of truth for tier semantics; `configs/staffing.toml` is the runtime bench mapping.
- `workflow/engine.py` owns Pi directive workflows and injects the junior delegate path.
- `pi_daemon.py` and `session_memory.py` provide resident local services; the terminal client is thin.

Read [docs/decomposition_dispatch.md](../../docs/decomposition_dispatch.md)
before changing planner, dispatcher, pow-wow scheduling, or subagent behavior.
Read [docs/completed/role_model_and_staffing_design.md](../../docs/completed/role_model_and_staffing_design.md)
before changing tiers, bench mapping, roles, or capacity.
Read [docs/saga_executor_modes.md](../../docs/saga_executor_modes.md)
before changing `/saga`, `/pow-wow`, executor modes, worktrees, or CLI launch behavior.
Read [docs/new_project_intake.md](../../docs/new_project_intake.md)
before changing `/start /new-project`, GAWD intake, or finalized draft approval boundaries.

## General Guidelines

These apply across Pi-dispatched agents unless the operator explicitly overrides
them for the current task:

- Never use the em dash. Use plain dash instead.
- When writing commit messages, never auto-add your agent name as co-author.
- Never manually modify `CHANGELOG.md` files or files marked as auto-generated.
- When writing or substantially editing long Markdown files, put each full sentence on its own line. Preserve normal Markdown structure, but avoid wrapping multiple sentences onto one physical line.
- When making technical decisions, do not give much weight to development cost. Prefer quality, simplicity, robustness, scalability, and long-term maintainability.
- When doing bug fixes, start by reproducing the bug in an E2E setting as closely aligned with end-user behavior as practical.
- When end-to-end testing a product, be picky about the UI you see. If something clearly looks off, try to get it fixed along with the task.
- Apply that same standard to engineering quality: lint failures, test failures, and test flakiness are real findings. If you see one, either fix it when it is safely in scope or record it clearly as a blocker/follow-up.

## Opinion And Voice Files

- If the task would benefit from Rahul/Kun's viewpoints, read `~/OPINIONS.md` when it exists. If it does not exist, say so and proceed from repo evidence.
- If you are drafting text as Rahul/Kun or under his identity, read `~/VOICE.md` when it exists. If it does not exist, avoid inventing a voice profile.

## Subagent Semantics

In this repo, a subagent is a scheduled tiered task, not necessarily an
in-process child agent.

- Junior: local Pi delegate, usually `gemma4`, no worktree.
- Senior: an external frontier coding CLI, the implementer; `configs/staffing.toml` names the vendor.
- Staff: the other vendor's frontier CLI, the reviewer. The two seats are never the same vendor.
- Code tasks get isolated worktrees.
- Advisory tasks are read-only/no-worktree.
- Coordination flows through ledger rows, artifacts, and `blocked_by`, not shared chat memory.

## Execution History Is A Query, Not An Inference

Before asserting that anything has or has not executed, read the durable record.
DBOS keeps every workflow and step this application has ever run, so "this has never run" is decidable rather than arguable, and an agent that reasons about it from the code will sometimes be confidently wrong.

```bash
uv run python agent_coordination_mcp.py --root <dir> read_execution_ledger --workflow-name execute_work_unit
```

It answers with workflow names, statuses, and counts only.
It cannot return a workflow's inputs, which for `durable_workflow_entrypoint` are ingress event payloads: the projection in `src/local_first_agent_os/durable_execution_ledger.py` is closed, and a process holding `LOCAL_AGENT_LEDGER_READER_DATABASE_URL` is additionally held to two columns by Postgres itself.

The record cannot answer what code will do the first time it runs.
There are no rows for code that has never executed, so a zero count is a finding about the past and not a prediction.

## Design Invariants

Apply the advanced software design principles this repo uses:

- Represent valid states in data: use typed contracts like `MiniGawdDoc`, `DecompositionPlan`, `PowWowTaskSpec`, `JudgmentRole`, `BenchSlot`, and `DispatchRunSummary`.
- Keep secrets in their module: tier-to-runtime mapping belongs in staffing config, scheduling belongs in the executor, durable state belongs in the ledger, and model/vendor choice belongs behind injected adapters.
- Preserve durable truth: if work is coordinated, record it in the ledger and artifacts, not only chat.
- Make dependencies explicit: use `blocked_by`; do not rely on prompt order or chat history.
- Crash or fail visibly on invalid planner/task output; do not silently coerce illegal states.
- Do not auto-merge, deploy, purchase, or send external communications without the approval gate.

## Work Pattern

1. Inspect the current code before proposing changes.
2. Identify which boundary owns the change: planner, dispatcher, executor, ledger, workflow, staffing, or runtime.
3. Prefer adding/changing data contracts over adding ad hoc branches.
4. Keep existing behavior unless the user explicitly asks to change it.
5. Add focused tests for the invariant being changed.
6. Run scoped checks first, then broad checks when touching shared paths.

## Context/Token Discipline

Senior and staff agents must keep exploration bounded:

- Start with a focused repo audit. Use file search before reading large files.
- Read only the files needed for the current phase.
- Do not paste long file contents into the response.
- Summarize findings with file paths and line references.
- Keep a compact running state: goals, decisions, files touched, commands run, blockers.
- Before broad exploration, state what you are looking for and why.
- Prefer small, verifiable increments over large speculative rewrites.
- When tests fail, report the smallest relevant error snippet, not the full log.
- If context is getting large, write a short checkpoint summary with current objective, completed steps, key files, commands/results, and next action.
- Do not re-read unchanged files unless needed.
- Do not re-litigate accepted architecture unless there is a concrete contradiction in the repo.

Common validation:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run ruff check
UV_CACHE_DIR=/tmp/uv-cache uv run pyright
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -k 'not streams_query_events'
git diff --check
```

## Handoffs

If work stops before completion, use `skills/handoff/SKILL.md`. A handoff must
say what was verified, what is unverified, exact changed files, and the next
command to run.
