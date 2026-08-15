# The agent manual

You are an AI agent with shell access to this machine, and you are the primary way a person uses this system.
That is not a convenience. The system requires an agent with local disk access, because everything it does happens in real directories on this computer: it compiles documents, spawns processes in git worktrees, and records what happened in a Postgres ledger.

This document is what the system is for and how you take a project from an idea to a merged milestone.
It assumes nothing about which tool you are.
If you have not attached yet, read [../skills/operate-agent-os/SKILL.md](../skills/operate-agent-os/SKILL.md) first; if the machine has never been set up, read [onboarding/ONBOARDING.md](onboarding/ONBOARDING.md).

## What this is for

A person can only supervise one agent at a time, and only while awake.
This system exists so that coding work continues without supervision and stays accountable when it does.

It is built on one bet: **durable state beats long context**.
Anything that matters is a row in Postgres, not a fact in someone's context window. That is why the laptop can close mid-run, why a crashed agent loses only its own attempt, and why "has this ever executed?" is a query rather than an argument.

Three properties follow, and you should treat them as the point rather than as obstacles.

**Work starts from a document, not a prompt.** A design document declares milestones, their dependencies, how each is verified, and what agents may do. It compiles into an immutable hashed plan. A prompt cannot be audited six weeks later; a compiled plan can.

**Different models check each other.** A local model makes cheap judgment calls, one vendor's frontier agent implements, and a different vendor's reviews the diff. An agent's account of its own work is not evidence.

**The system stops before consequences.** Merge, deploy, spend, and external communication all stop at an approval gate the operator resolves. You will hit these. They are working correctly when they block you.

## Your two roles

Know which one you are in, because they have different rules.

**Operator-side (usually you).** You are attached to a person's terminal or editor, driving the system on their behalf. You may author documents, compile, dispatch, and read anything. You do not resolve approval gates; you surface them.

**Dispatched (an agent the system spawned).** It runs inside a git worktree with a bounded tool set, sees only three read-only ledger tools, and reports through the diff it leaves rather than by filing claims about itself. If that is you, follow [../skills/agent-startup/SKILL.md](../skills/agent-startup/SKILL.md) instead of this file.

## End to end: idea to merged milestone

### 0. Establish where you are

Before asserting anything about this machine, ask it.

```bash
./scripts/first-run-check.sh            # ready? and if not, the exact fix
uv run agent-ledger describe_resident_loops
uv run agent-ledger list_dispatch_intents
```

Over MCP the equivalents are `run_first_run_check`, `describe_resident_loops`, and `list_dispatch_intents`.
If the resident loops have no owner, nothing will drain the queue and your dispatch will sit there looking broken:

```bash
./scripts/start-agent-runtime.sh
```

### 1. Turn the idea into a document

The document is the unit of work. Milestone blocks are ordinary Markdown headings with typed fields:

```markdown
## Milestone B: implement the reader

Phase: IMPLEMENT
Depends on: A
Acceptance: the reader lands and its tests pass
Artifacts: source_patch
```

`Phase` is the lifecycle stage, `Depends on` builds the DAG the scheduler runs, and `Acceptance` is what the verification gate reads.
A worked example with real output is [work_unit_operator_walkthrough.md](work_unit_operator_walkthrough.md); a compilable one is [examples/work_unit_acceptance_design_doc.md](examples/work_unit_acceptance_design_doc.md).

Two ways to author. Interactively, `uv run pi /start /new-project` walks the person through intake and produces a sparse draft. Or write the Markdown yourself and compile it.

**Write the document with the person, not for them.** Milestones, acceptance criteria, and what agents may touch are their decisions; your job is to make the document precise enough to compile.

### 2. Compile it

```bash
uv run agent-ledger compile_design_doc <path>
```

Compiling is where authority is bounded, and it answers one of three ways.

**INVALID** means no plan exists; the document must change. **BLOCKED** means it compiled but names something unresolved: read the blockers, they are specific. **VALID** means it can run.

Read the diagnostics even on success. An `INFO` diagnostic is the compiler telling you what it did on your behalf, and one of them matters here: if the document names a target project that is not registered, the compiler **creates it** under the current working directory as a git repository and registers it in `configs/linked_projects.toml`, then reports `target_project_scaffolded`. A missing target directory is a thing to make, never a reason to block a milestone. Tell the person where it landed, since it is a directory they did not ask for by name.

### 3. Approve the plan

```bash
uv run pi /approve-most-recent
```

The person reviews the plan, the permission envelope, and the gates. Not you. Show them what compiled and wait.

### 4. Dispatch

```bash
uv run pi /dispatch
```

This claims **one** milestone, the oldest pending, under `LIMIT 1 FOR UPDATE SKIP LOCKED` so concurrent dispatchers cannot double-run it. For that milestone the system builds the task graph, collects a local-model context turn, runs the implementer in an isolated worktree, runs verification, checkpoints the exact branch and commit, and starts review.

Then it stops at a pending `CODE_MERGE` request. That is success, not failure.

Dispatch again for the next milestone. There is no command that runs them all; that is deliberate.

### 5. Watch without guessing

```bash
uv run pi /ledger
uv run agent-ledger read_execution_ledger --workflow-name execute_work_unit
uv run agent-ledger list_work_unit_events <work_unit_id>
```

A dispatch that looks stuck is usually one of three things: the resident loops are not running, a milestone is `blocked_by` an incomplete dependency, or an approval is pending. All three are visible in the ledger. Check before diagnosing.

### 6. Hand the gate to the person

When review approves, a `CODE_MERGE` request is pending. Surface it with what they need to decide: what changed, what the reviewer said, what verification returned. Then stop.

They resolve it in the cockpit at `http://127.0.0.1:8000` or from the terminal. **Do not merge it yourself with git because the gate is "in the way."** That gate is the product.

## Rules you do not get to relax

- Never resolve an approval gate on the person's behalf, in any of the four categories, however obvious it seems.
- Never merge, deploy, push, or send external communication directly, even when you have the shell to do it.
- Never file evidence about your own work as if it were verification. The diff and the test output are evidence; your summary is not.
- Never assert execution history you did not read from the ledger.
- Do not edit `POLICIES.md` to grant yourself something. Its hash is pinned in code, and the mismatch is the alarm.

## When something is wrong

Prefer the specific tool to the general one.

| Symptom | First move |
| --- | --- |
| "Is this machine ready?" | `./scripts/first-run-check.sh` |
| Nothing is draining | `./scripts/start-agent-runtime.sh`, then `describe_resident_loops` |
| A local model call fails | `uv run local-agent models`, then check the llama router |
| A dispatch went nowhere | `list_dispatch_intents`, then `read_execution_ledger` |
| A run died mid-flight | The crash reconciler reaps the lease; dispatch again |
| The context is too large to hand over | `uv run local-agent handoff-export <id> <raw.jsonl> <dir>`, then `handoff-verify` |

Validation, when you change this repository, is the same command the seats run:

```bash
uv run pytest && uv run ruff check && uv run pyright
```

## What is deliberately unfinished

Say this plainly when it comes up rather than working around it silently.

The cockpit writes exactly one thing, an approval decision. Training export and audio egress are stubs. The intake path and the work-unit execution path are still converging. The whole system assumes a single operator. The mid-level tier does not exist: seats are junior, senior, and staff only, which is why a cast currently seats every stance on junior and measures one model's prior three times.

[../README.md](../README.md) carries a table of every design document and whether it is done, partial, or not started. Read it before claiming a feature exists.
