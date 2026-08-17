# Design tradeoffs

This document walks through the load-bearing design decisions in this system, what each one buys, and what each one costs.
It is written for a reader evaluating the architecture, not for an agent resuming work.
For the package-level map, read [code_structure.md](code_structure.md).
For the honest current-state snapshot, read the README's Status section.

## 1. Durable ledger over chat state

Every unit of coordinated work is a row: sagas, milestones, tasks, artifacts, evidence, approval requests, dispatch intents, and execution leases live in Postgres.
SQLite is retained only as a disposable test adapter under `potential_directions/sqlite_test_adapter/`.
Chat context is treated as a lossy cache over that ledger, never as the source of truth.

What it buys: crash recovery, resumability, and auditability.
A dispatcher can die mid-milestone and `reconcile_saga_milestones` repairs state from terminal dispatch intents instead of from anyone's memory of the conversation.
Multi-day projects survive across sessions, machines, and models because nothing essential lives in a context window.

What it costs: ceremony.
Every state transition is a command against the ledger, which is slower to write and more verbose than mutating in-process objects.
The bet is that for unattended agent work, the audit trail is the product, so the ceremony is the point.

## 2. A contract, not a prompt, as the unit of work

Work enters through a GAWD doc: a scoped build contract with explicit milestones, non-goals, verification requirements, and a permission envelope.
Models expand a sparse human draft into the full contract, and a human approves it before any execution.

What it buys: the permission envelope makes "what is this agent allowed to do" a reviewed artifact instead of an emergent property of prompts.
Milestones give the saga durable resume boundaries and give humans deliberate gates.
When later evidence invalidates a pending milestone, the audited amendment operation changes the contract with a reason, an actor, and before-and-after evidence, instead of silently editing history.

What it costs: startup latency.
A one-line idea takes minutes of intake before any code is written.
For interactive exploration that overhead is wrong, which is why the imperative `/saga` door exists alongside the contract path.

## 3. The CLI subprocess as the coordination transport

Every agent, whether Claude, Codex, a local model, or a human in a terminal, talks to the ledger through the same CLI door.
Production orchestration constructs typed command variants, and `coordination/transport.py` is the single module that lowers a variant to argv, runs the process, and parses the JSON envelope back into a typed result.

What it buys: cross-agent parity.
There is exactly one command surface to secure, test, and document, and any harness that can run a subprocess can participate without a bespoke SDK.
Durable DBOS payloads can serialize commands as argv and replay them years later against the same validated vocabulary.

What it costs: two transports that must stay in parity.
The in-process transport is now the default for production orchestration, because the subprocess spawn overhead stopped paying rent once the command surface stabilized; the subprocess transport remains the door for external harnesses and for any process that is not this one.
Going in-process surfaced a hazard the subprocess model had hidden: a child gets its own environment, but one process has exactly one, so two commands aimed at different ledgers would race on shared selection state.
`coordination/ledger_selection.py` is the answer - the four values that pick a ledger travel as one applied selection behind a barrier, because a sticky selection with no barrier is a data race whose symptom is a query against the wrong database, which no assertion downstream would catch.

## 4. Typed command and result sums instead of stringly argv

The command API is a union of frozen data variants such as `CreateSaga`, `SubmitArtifact`, and `AmendSagaMilestone`, with enums like `CoordinationCommandName` and the terminal-status vocabularies as discriminants.
Enums alone were rejected because an enum member cannot carry a command's required fields or make invalid field combinations unrepresentable.
Known responses are parsed once into the `EntityResult | CollectionResult | AcknowledgementResult` sum before orchestration callbacks consume them.

What it buys: parse-don't-validate at the coordination boundary.
Constructing an invalid command is a type error at the call site, not a runtime CLI usage failure three processes away.

What it costs: the sum is not yet complete.
Entity bodies inside results are still mappings rather than a union of domain records, and some legacy tests still build raw argv lists.
The boundary is typed; the payload interiors are the follow-on seam.

## 5. Model output is parsed at the boundary, never sniffed downstream

Free-form model text is converted into finite decisions exactly once, at the edge where it arrives.
`ReviewVerdict.parse()` turns the first reviewer decision line into one of `APPROVE`, `REQUEST_CHANGES`, `REJECT`, `ESCALATE`, or `UNCLASSIFIED`, keeping the original text as evidence.
`TaskPurpose` does the same for legacy task strings.
Downstream control flow matches enums instead of re-sniffing strings.

What it buys: one vocabulary per decision, and a visible `UNCLASSIFIED` outcome instead of a silent misroute when a model improvises its answer format.

What it costs: the parser is a bottleneck that must evolve whenever the prompt protocol does, and prose that resists classification degrades to the conservative branch rather than being cleverly interpreted.

## 6. Prompt loss is data: ViewBlock

Prompts derived from durable sources are built from `ViewBlock` values: `VerbatimViewBlock`, `CompactedViewBlock`, or `TruncatedViewBlock`.
Every lossy variant carries its source label and original size, and rendering visibly labels the loss.

What it buys: the executor can distinguish exact evidence from a derived view before text reaches a reviewer, and the complete ledger artifact remains the truth a later policy can re-derive from.
Hidden string slicing was the alternative, and it hides exactly the information a reviewer needs to distrust a summary.

What it costs: a second model call on the prompt path, bounded so it cannot become the problem it solves.
Compaction is now chosen where overflow hurts most: the dependency-context block, where truncation drops the most recent upstream task - the one the agent most likely builds on.
An injected compactor summarizes the whole block through the resident local model, and every way a summarizer can disappoint - unreachable, empty, over budget, or the well-formed garbage the mock models produce - falls back to the truncation this system shipped first, labeled as such.
The call pays an advisory timeout budget rather than inheriting the frontier agent's hour, because it runs before the execution lease opens, where a stall is invisible to the supervisor.

## 7. Tiered execution with bounded failover

Tasks route by tier: junior work fans out to free local models with no worktree, senior implementation runs `claude --print` in an isolated worktree, and staff review runs `codex exec` against the same worktree.
On a timeout or rate limit, the executor makes exactly one bounded cross-provider attempt, and a frontier judgment is never silently downgraded to the local junior model.

What it buys: cost and quality routing under one scheduling model, decorrelated implement-then-review across vendors, and swappable models per tier in config with no code change.

What it costs: tier semantics are one more vocabulary to maintain, and the single-failover rule means some recoverable situations fail closed on purpose.
Failing closed was chosen because an invisible quality downgrade is worse than a visible failure in unattended operation.

The bench is also no longer fixed at process start.
Each claimed intent re-resolves its staffing against the ledger's record of spent quotas, because the alternative was observed, not imagined: one harness reported a usage limit at 03:04 and the 03:44 dispatch went to it anyway.
The read is bounded to a two-second checkout budget and answers empty on any failure, so losing the optimisation never becomes a refusal.
The visible cost is that restaffing around a spent quota can put the implementer and the reviewer on one provider, collapsing the two-model cross-check; the dispatch path names that collapse in its progress events rather than preventing it, because one provider checking itself is still better than a run stranded mid-flight.

## 8. Verification is a sum, not a boolean

`all(capture.exit_code == 0)` is `True` on an empty tuple, and for a while that meant a project declaring `verification_commands = []` produced a checkpoint commit, a completed task, and a merge approval having verified nothing.
The gate now answers with a four-member sum - not declared, incomplete, passed, failed - and only `VerificationPassed` permits a checkpoint, matched exhaustively so a member added later without a decision is a type error rather than a silent default.
The registry refuses to load a writable project that declares no verification commands at all, so the false-green state cannot even be authored.

What it buys: everything downstream that trusts "verification passed" is finally trusting something.

What it costs: friction at intake.
A writable project must say how its work is checked before it can take any, and a run parked before verification refuses to certify without being marked failed, which is a subtler state operators must learn to read.

## 9. Resident loops are database singletons under OS supervision

The two processes that make queued work move - the enqueue drainer and the ledger dispatcher - own their role via a Postgres advisory lock, not a pid file, because they are singletons over the coordination database rather than over a checkout.
launchd supervises both with `KeepAlive`, and the lock turns the supervised copy into a standby: it exits immediately while another copy holds the loop, and takes over the moment that copy stops.

What it buys: a reboot is not a manual step.
The machine comes back with its services healthy and its queue draining, and a loop that dies on a defect is restarted by the operating system rather than by an operator noticing.

What it costs: the database URLs are spelled twice - as the start script's defaults and again in the plists, since launchd inherits no shell - and a tripwire test is what keeps the two spellings agreeing.
Supervision runs in the login domain, so after a power cycle the queue resumes at login rather than at power-on.
The crash reconciler is deliberately not supervised: automatic retries are only safe once a spawned agent's authority is bounded by what its plan declared, so turning it on remains an operator's decision.

## 10. Irreversible actions fail closed behind human gates

Merges, deploys, spending, secret access, and external communications create pending approval requests in the ledger and block until an operator resolves them.
Approval is scoped: approving milestone execution does not approve the resulting merge, which raises its own `CODE_MERGE` request.

The sharpest instance is deliberate non-automation: the `MERGE_APPROVED -> MERGED` transition is performed by nothing.
The approve path composes the exact `git merge --ff-only` command and prints it for the operator, because an automated single-branch fast-forward would remove the reader without adding a gate.
The refinery integration queue - batch selection, bisect blame, the request lifecycle - is built and tested as the mechanism that will eventually perform it, and as of 2026-08-08 its design is complete: the batch-formation rule it was blocked on turned out to be a resident loop whose batch window is the duration of the previous run rather than a configured wait. What remains is the runner and the git plumbing, which is the acknowledged seam, held open on purpose rather than closed badly.

What it buys: the system can run unattended without the operator trusting it with anything irreversible.

What it costs: a human is a latency floor on every irreversible edge, by design, and today that includes every merge.

## 11. Resource budgets and claim safety live in the contract

Two live incidents shaped this rule.
A site build exhausted workstation memory while local models were resident, and early business records mixed verified facts with unverified marketing claims.
Both fixes were encoded as amended milestone contract terms, with heap caps, worker limits, peak-RSS evidence, claim provenance, and testimonial permission flags, rather than as operator folklore.

What it buys: the constraint is tested and evidenced by the factory itself, and it survives handoffs because the ledger carries it.

What it costs: contracts grow operational detail that a purist would call deployment configuration.
The position here is that a budget nobody verifies is a wish, and the milestone's evidence requirement is the verification.

## 12. Residual debt, stated plainly

The refactor into lifecycle packages named the boundaries but did not make every module small.
`pow_wow/executor.py` is roughly 4,000 lines, `workflow/engine.py` roughly 2,300, and `coordination/cli.py` and `coordination/contracts.py` are each past 1,200.
Result entity bodies are still mappings.
These are held as bounded follow-on seams to be opened when a real task hits them, because the last speculative rewrite this repo avoided is the reason the current architecture is trusted.

The honest ledger of the rest is a 2026-08-07 audit of this repository: an inventory of mechanisms that are built, tested, and consulted by nothing.
Two of its entries have since closed - the verification gate above, and dispatch-time availability - and the pattern the audit names is the debt that matters here: this codebase's failure mode is not code that breaks but code that works and is never called.
Still open from that inventory: the refinery runner and the `MERGED` transition it will perform, a tool-permission revocation with no operator surface and a request-grant loop that cannot lift it, a gated real-world-action layer with zero constructors, the promised OBSERVING-posture rollup, and two recovery loops that are invocable but scheduled by nothing.
