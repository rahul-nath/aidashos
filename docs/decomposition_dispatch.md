# Decomposition and Dispatch

The control plane separates three decisions:

- Decomposition decides what should be delegated.
- Dispatch decides when a durable intent is claimed and resolved.
- Execution decides how each task runs against a tiered bench.

This keeps the planner prompt as a front-end, not a replacement substrate. A
planner emits a `decomposition_plan.v1` artifact containing two typed parts:

- a scoped `mini_gawd_doc.v1` brief that records the theory, golden flow, scope,
  non-goals, core lifecycle, top failure, verification, and decision log;
- a task DAG made of `PowWowTaskSpec` values.

The dispatcher stores each task in the coordination ledger, including its
`blocked_by` dependencies, then hands the DAG to the pow-wow executor. The
executor remains the only component that schedules by dependency, tier capacity,
worktree group, and dispatch kind.

## Valid States

A valid decomposition has:

- a `mini_gawd_doc.v1` scoped design brief;
- at least one task;
- unique task names;
- only dependencies that reference other tasks in the same plan;
- no dependency cycles;
- a concrete tier for every judgment task;
- an explicit `dispatch_kind` from the canonical `coordination.contracts.DispatchKind`
  enum: `advisory`, `code`, or `cast`.

`advisory` produces read-only judgment work, `code` produces the governed
implementation and review pipeline, and `cast` produces independent named
stances followed by a synthesizer that depends on every stance.

Invalid planner output raises before any executor runs.

## Independent-before-junior planning

Every code decomposition carries a typed five-phase planning visibility contract.

1. `senior_independent_reading` reads the raw operator contract and repository with no dependency output.
2. `staff_independent_reading` separately reads the same raw boundary with no junior or senior conclusions.
3. `junior_verification_plan` starts only after the senior reading has crossed a durable artifact barrier.
4. `senior_owned_plan` receives the independent reading plus the junior's non-exhaustive hypotheses and owns the final implementation and verification plan.
5. `staff_final_review` receives its independent reading plus the senior-owned implementation and remains responsible for the final verdict.

These phases are `PlanningPhase` values persisted on `PowWowTaskSpec`, not role-name or prompt conventions.
The executor validates the exact tier, purpose, dispatch kind, dependency edges, and shared review worktree before it launches a model.
It rejects unphased judgment tasks in the same planning-enabled graph so an alternate model turn cannot bypass the visibility contract.

Each successful phase is submitted immediately as `planning_evidence.v1` against its durable task row before a dependent model can start.
Persistence failure fails the phase and blocks downstream work.
The junior artifact is stamped `non_exhaustive=true`; it cannot narrow the raw contract, delete a senior concern, or become approval evidence.

The mini-GAWD brief is deliberately not a second execution stage. It is the
planner's typed guardrail against intent drift: enough design structure to keep
scope honest, but not the full 15-section GAWD process for every dispatch.

## Current Subagent Semantics

In this repo, a subagent is not specifically an in-process agent. It is a
scheduled task assigned to a tier. The tier resolves through `configs/staffing.toml`
to an execution mode:

- junior: local Pi delegate, usually `gemma4`, no worktree;
- senior: external `claude` CLI process, worktree for code tasks;
- staff: external `codex exec` process, read-only for review tasks.

Coordination happens through the ledger and artifacts, not process namespace or
shared chat state. Dependency output is fed to downstream task prompts through
`blocked_by`.

## Requirement Boundary

The custom substrate is justified when these requirements are active:

- durable ledger state;
- cross-vendor execution;
- local model delegation;
- worktree isolation for code work;
- approval gates for irreversible actions;
- event-driven dispatch from queued intents.

If the only requirement were "coordinate hosted Anthropic subagents inside one
managed session", a managed-agent cookbook would be simpler. This system exists
to keep the above requirements real.
