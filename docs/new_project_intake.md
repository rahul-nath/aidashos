# New Project Intake

`/start /new-project` is the file-first GAWD intake path. It replaces raw
`/saga "build X"` as the safer front door for new build work.

## Flow

Create a sparse draft file:

```bash
pi /start /new-project --target-project-id ai_business_portfolio
```

Pi writes a human-editable template under:

```text
docs/gawd_drafts/gawd_doc_<id>.txt
```

Fill in the mini-GAWD sections as sparsely or completely as needed, then ingest
the file. The template includes a compact "full-GAWD expansion" surface:
execution milestones, operational contract, rollout/migration/rollback, and
risk/limitations. Leave those sparse when you want senior/staff agents to fill
them from your intent.

Section 14, `Permission Envelope`, is different: it is a declaration, and it
ships filled in with the safe default rather than left blank. See
[Permission Envelope](#permission-envelope) below.

```bash
pi /start /new-project docs/gawd_drafts/gawd_doc_<id>.txt --target-project-id ai_business_portfolio
```

## GAWD Walkthru

Use the walkthru when speaking through the project is easier than editing
every line of the template:

```bash
pi /start /new-project --walkthru --target-project-id ai_business_portfolio
```

The command stays in the foreground for the whole interview. Pi asks one
question, blocks for the answer, durably saves it, proposes a summary, and
blocks again for accept or revise before advancing. The operator does not copy
continuation commands or carry the `walkthru_id` between turns.

For each section, Pi keeps three different things:

- the operator's verbatim answer;
- a faithful model-proposed summary;
- model suggestions and inferences, which never enter the contract implicitly.

At an answer prompt:

- type the answer normally;
- type `/skip` to keep that sparse section unchanged;
- type `/pause`, or press Control-C, to leave the saved interview.

At a summary review prompt, Enter accepts it, `r` replaces only the summary,
and `p` pauses. After the last section, Enter renders the draft or `e` edits an
accepted section first.

Running the same bare command later discovers the newest unfinished local
interview and offers to resume it:

```bash
pi /start /new-project --walkthru
```

The explicit `--answer`, `--accept`, `--revise`, `--skip`, `--edit`, `--status`,
and `--finish` transitions remain available for scripts and non-interactive
clients, but they are not the normal terminal UX.

Walkthru state is stored under `docs/gawd_drafts/` beside the private draft,
so it survives terminal and daemon restarts. `--finish` does not create a saga,
generate milestones, approve anything, or start implementation. It prints the
unchanged ingest command for the rendered sparse draft; the flow below begins
only when the operator runs that command.

The ingest path:

- parses the sparse GAWD draft;
- carries any explicit execution milestones or full-GAWD expansion notes into
  the typed draft payload;
- creates a saga in the coordination ledger;
- creates an initial ledger GAWD doc for the sparse draft;
- creates a `GAWD_DOC` pow-wow with:
  - `junior_permissions_scan`, which advises on permissions the draft did not
    declare; it does not produce the envelope;
  - `senior_spec_completion`;
  - `staff_final_verdict`;
- runs that pow-wow through the configured saga executor;
- writes `gawd_doc_<id>.finalized.md`, carrying the derived milestones, a
  `## Required Artifacts` delivery contract, and the senior and staff turns
  quoted rather than pasted;
- writes `gawd_doc_<id>.permissions.json`;
- creates a finalized ledger GAWD doc as `DRAFT`, carrying the durable workflow
  plan in its `task_graph` payload;
- makes the saga point at that finalized `gawd_doc_id`;
- stops before implementation.

## What The Finalized Document May Not Contain

The finalized file is the document `compile_design_doc` reads, so anything in it
is a claim the compiler will act on.

The senior and staff turns return free markdown, and that output is merged into
this file. It is blockquoted first. A model asked to expand a spec restates that
spec's headings, which gave the document a second `## Permission Envelope` and a
`duplicate_permission_envelope` rejection: a spec made uncompilable by the
transcript of how it was written. The sharper case is a `### Milestone N:` block
inside that prose, which parsed as a real milestone, so a model could add an
executable step to a plan by describing one. A `>` prefix settles both, because
the heading and field patterns are anchored at line start.

Fenced text is quotation too. The parser masks code fences before it looks for
sections, milestones, fields, or permission actions, so a fenced example - the
template's own `### Milestone 0:` block, or the envelope syntax shown in this
document - shows a shape without declaring one. Before this rule, every sparse
draft compiled with a fake PLAN milestone copied from the template's example.

## One Document Grammar

The sparse draft, the finalized document, and every design document in `docs/`
are one format read by one parser: `split_document_sections`, `mask_fences`,
`normalize_heading`, and `canonical_heading` in `work_units/design_doc.py`.
`parse_sparse_gawd_draft` selects sections through the same functions the
compiler uses, so the two cannot disagree about where a section starts, what a
heading means, or where a fence is. The intake module's private section parser
is gone.

Headings match the canonical vocabulary exactly after normalization, or not at
all. An accepted variant spelling classifies through the alias table and emits
an `alias_heading` diagnostic naming the canonical form. Any other heading is
UNKNOWN: preserved, named, and reaching no compiler collection. There is no
substring matching, so a heading that merely contains a known phrase -
"Permission Envelope (Proposed)", "Not in scope" - no longer becomes that
section.

The template is the format's executable spec.
`test_the_blank_template_parses_with_nothing_unaccounted_for` renders the
shipped template, parses it, and pins the result: every level-2 heading known,
no diagnostics beyond `no_milestones`, and the fenced example inert. Change the
template or the vocabulary alone and that test names the drift.

`## Required Artifacts` is derived from the phases the milestones declare, and
is the plan's delivery contract. The compiler rejects a plan that declares an
IMPLEMENT milestone and names no terminal evidence, and intake used to emit
neither this section nor a DELIVER milestone, so every finalized document failed
to compile on a plan intake had just written. A document that states the section
itself keeps it; the derivation never overwrites an operator. No DELIVER
milestone is invented, because whether work ships is the operator's decision.

Sections whose body the compiler reads as a list take list entries and nothing
else. `_bullet_lines` keeps every non-empty line that is not a heading, so a
sentence introducing `## Required Artifacts` becomes an entry in it, and the
plan then requires evidence no executor produces.

A milestone's `Acceptance:` lines are its own exit gate, not the document's.
Document-wide Verification, Operational Contract, and Risk Synthesis sections
already reach the compiler through the canonical heading table in
`work_units/design_doc.py`; copying them onto every step carried them twice and
asked a planning milestone to satisfy an on-device check.

## Permission Envelope

The envelope is declared by the document, the way milestones are.

`compile_design_doc` finds a milestone by its `### Milestone N:` heading and its
typed `Phase:` / `Acceptance:` / `Artifacts:` fields, and by nothing else.
That rule exists because inferring milestones from punctuation once turned one
GAWD doc's fourteen ordinary sections into fourteen fake milestones.
Permissions were still inferred, from substrings of the draft prose, and
produced the same class of result.

A real offline iOS draft requested `dependency_install`, `network_access`,
`deploy`, and `spend_money`.
All four came from word senses the scan cannot see: "install" inside "the app
installs on the physical iPhone", "paid" inside "paid Apple Developer Program
membership", "deploy" inside "Deploy is an Xcode build-and-run", "api" inside an
interface-contract sentence.
The same document says it has no API spend and no network dependency at runtime.
`deploy` and `spend_money` are the two permissions that decide whether an agent
may ship or spend, so asking for them on a document that disclaims both is how
an operator learns that approving is the default answer.

### The three lists

Section 14 of the template carries them, and `compile_design_doc` reads the same
three labels out of any design document:

```markdown
Autonomous permissions:
- read_repo_context

Requested permissions:
- code_worktree_write: reason the operator reads when deciding

Denied without explicit approval:
- deploy
```

The vocabulary is closed; it is `PermissionAction` in
`src/local_first_agent_os/work_units/permissions.py`.
An unrecognized action name is a compile error rather than a skipped line,
because ignoring a grant and ignoring a denial are both changes to what an agent
may do.
`Requested permissions:` is what makes `requires_start_approval` true, so a
document that asks must have its exact compiled plan hash approved at start.

### When the section is absent

Deleting the section is allowed and means the baseline, which is the same
`BASELINE_*` tuples the template is rendered from:
read the repo, write an isolated worktree, run the declared tests, record ledger
artifacts, and ask the operator.
Everything irreversible sits outside that ceiling.

A milestone whose executor needs a capability the ceiling withholds becomes an
execution blocker naming the action to declare, rather than an executor that
runs and quietly does nothing.

The baseline's two build actions are autonomous rather than requested, so a
document that declared nothing adds no start approval.
This is strictly tighter than what an undeclared document used to get, which was
no ceiling at all.
A gate on every document is a gate an operator learns to clear without reading,
which is the same failure as over-requesting.

### The keyword scan

The substring scan survives as a suggestion and grants nothing.
It runs over the draft prose, skipping the Permission Envelope section and any
HTML comments, and prints what it found under `Suggested by keyword scan:` in
the finalized document, next to the term that triggered it:

```text
Suggested by keyword scan:
- network_access: Draft appears to require external lookup or networked services. (matched: api, download, network)
```

A suggestion whose capability the envelope already settles is dropped rather
than repeated.
Read a suggestion as a prompt to edit the three lists, and check its matched
term before believing it.

`gawd_doc_<id>.permissions.json` is `permission_envelope.v2`.
It gained `source` (`declared` or `baseline`) and `suggestions`, and the two
together changed what `risks` used to mean: under `v1` every envelope was
heuristic and the risk note said so.

## Approval Boundary

Finalization is not approval. The finalized GAWD doc, permission envelope, and
durable workflow plan are operator-review artifacts. Two of the three are files
on disk; the plan is read from the finalized doc's `task_graph`. Start
implementation only after reviewing the finalized file and the permission
envelope beside it:

```bash
pi /approve-most-recent
```

The shortcut resolves the most recently active dependency-ready milestone,
its finalized GAWD doc, and its embedded target from the durable ledger, then
prints those resolved IDs. Use the explicit form below only to select a
different document or target:

```bash
pi /start /approved-gawd <final_gawd_doc_id> --target-project <project_id>
```

When `--target-project-id` was supplied during intake, the finalized task graph
retains it and the approval command can infer the target. Supplying
`--target-project` again remains an explicit override/check.

That command approves the finalized ledger GAWD doc if it is still `DRAFT` and
submits one durable code dispatch intent with source
`approved_gawd:<final_gawd_doc_id>`. Code dispatch is fail-closed: if no target
project is provided by the command or embedded in the finalized task graph, no
implementation intent is queued. It does not run agents inline. The
dispatcher/reactor claims and executes the pending intent once the target is
explicit:

```bash
pi /dispatch
```

Rerunning the approval command for the same doc is idempotent while an existing
pending, claimed, or completed dispatch intent exists.

To recover a terminal attempt without remembering its milestone ID, run:

```bash
pi /try-milestone "reason for retry"
pi /approve-most-recent
pi /dispatch
```

The first command selects only the most recently updated `FAILED`, `BLOCKED`,
or `CANCELED` milestone and returns it to `PENDING`; it does not enqueue or
start an agent.

If an intent was queued against the wrong target, cancel it before enqueueing a
replacement, or supersede it in one ledger transaction:

```bash
python agent_coordination_mcp.py --root ~/.local-agent/coordination/local_first_agent_os \
  cancel_dispatch_intent <intent_id> --reason "wrong target"

python agent_coordination_mcp.py --root ~/.local-agent/coordination/local_first_agent_os \
  supersede_dispatch_intent <intent_id> --target-project-id <project_id> \
  --reason "route to explicit target"
```

## Durable State

The text files are the human editing surface. The coordination ledger is durable
truth after ingestion:

- `sagas.gawd_doc_id` points to the active finalized draft;
- `gawd_docs` keeps the sparse and finalized versions;
- approved finalized docs enqueue implementation through `dispatch_intents`;
- `pow_wows`, `saga_tasks`, and `task_artifacts` record the finalization run;
- the permission envelope is stored both as a sidecar JSON file and a pow-wow
  artifact.
- the durable workflow plan is stored in the finalized doc's `task_graph` and as
  a pow-wow artifact. It has no sidecar file: one was written until 2026-08-11
  and nothing ever read it.

## Durable Workflow Plan

The durable workflow plan is the bridge from Mini GAWD to generated DBOS
workflow code. `configs/durable_workflow_plan.toml` is the reusable contract.
Each finalized intake fills it and stores the result in the finalized GAWD doc's
`task_graph` under `durable_workflow_plan`.

The plan prefers explicit `Execution Milestones` when the operator supplies
them. If that section is sparse and the draft is SaaS-shaped, the deterministic
SaaS archetype planner supplies candidate milestones and approval gates. If no
archetype applies, the plan uses `Happy Path / Golden Flow` to create ordered
milestones dynamically. Senior/staff agents then expand missing full-GAWD
concerns before approval:

- `Core Design` supplies unit of work, lifecycle, data model, inputs, outputs,
  and side effects;
- `Operational Contract` supplies service levels, input bounds, interface
  contracts, idempotency/replay, observability, dependencies, security/access,
  and backpressure/cost controls;
- `The Failure That Matters Most` supplies retry, timeout, rollback, or
  fail-closed behavior;
- `Verification` supplies evidence artifacts and smoke checks;
- `Rollout / Migration / Rollback` supplies approval gates and compensation
  behavior;
- `Risk Synthesis / Known Limitations` supplies block conditions, risk gates,
  and revisit triggers;
- `Permission Envelope` supplies allowed capabilities and denied actions. Senior
  and staff expand the other sections; this one they do not invent, because it
  is what the operator declared or the baseline the operator left standing.

The SaaS archetype planner is a compile-time scaffold, not new runtime state.
It contributes reusable execution templates for internal ops tools, B2B
workflow SaaS, self-serve productivity SaaS, marketplace/lead platforms,
data/analytics SaaS, AI workflow SaaS, integration/automation SaaS, developer
tool/infra SaaS, commerce/billing SaaS, regulated/sensitive-data SaaS, and
deploy/release gates. The compiled result is still the durable workflow plan;
after approval, the existing ledger tables persist the runtime state as
`saga_milestones`, `dispatch_intents`, and milestone evidence.

This is the intended compile path:

```text
Sparse Mini-GAWD
  -> archetype planner adds candidate SaaS milestones when useful
  -> staff expands missing full-GAWD concerns
  -> durable milestone plan
  -> operator approval
  -> milestone-scoped execution
```

Each `[[steps]]` entry records:

- `step_id`
- `name`
- `source_sections`
- `durable_boundary_reason`
- `inputs`
- `outputs`
- `side_effects`
- `idempotency_key`
- `retry_policy`
- `timeout_policy`
- `compensation_or_rollback`
- `approval_required`
- `evidence_to_record`

Generated DBOS workflow code remains a separate approval boundary. It should be
created in an isolated worktree or PR with tests, reviewed, merged, and loaded
only after runtime restart. It must not be hot-loaded into the resident daemon.
