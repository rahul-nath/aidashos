# WorkUnit Operator Walkthrough

One worked example from a DesignDoc to a completed WorkUnit, with the real parsed representation, the real compiled plan, and the real event trace.
Every output below was produced by the code on this branch against a disposable ledger.

Commands are typed as `agent-ledger`, the `[project.scripts]` entry for `local_first_agent_os.coordination.cli:main`.
`./scripts/install_pi_shell.sh` puts it on `PATH`; `uv run agent-ledger ...` from the repo root is the same command, as is `uv run python agent_coordination_mcp.py ...`, which imports the same `main`.

## 1. The example DesignDoc

Milestone blocks are ordinary Markdown headings with typed fields.
Nothing else in the document is required to be machine readable, and nothing in it is discarded.

```markdown
# Acceptance design doc

## Requirements

- Compile one DesignDoc revision into one immutable plan.

## Constraints

- A document may not supply executable code.

## Acceptance criteria

- The seven lifecycle phases occur in their fixed order.

## Non goals

- A general workflow language.

## Milestone A: plan the change

Phase: PLAN
Acceptance: a written implementation plan exists
Artifacts: implementation_plan

## Milestone B: implement the reader

Phase: IMPLEMENT
Depends on: A
Acceptance: the reader lands
Artifacts: source_patch

## Milestone C: implement the writer

Phase: IMPLEMENT
Depends on: A
Acceptance: the writer lands
Artifacts: source_patch

## Milestone D: verify the suite

Phase: VERIFY
Depends on: B, C
Acceptance: the suite passes
Artifacts: test_result

## Milestone E: staff review

Phase: REVIEW
Depends on: D
Executor: review.operator
Approval: required
Acceptance: an operator approved the change
Artifacts: operator_approval

## Milestone F: deliver the artifact

Phase: DELIVER
Depends on: E
Acceptance: the delivery record exists
Artifacts: delivery_record
```

The file above is `docs/examples/work_unit_acceptance_design_doc.md`, and `tests/work_unit_support.py` reads that same file.
There is one copy, so this example cannot drift from what the code accepts.

## 2. The parsed representation

`parse_design_doc` reports what the document says and where it says it.
It applies no policy: the phase is read, never guessed, and an unrecognized section is preserved rather than dropped.

Milestone A as parsed:

```json
{
  "declared_key": "a",
  "title": "plan the change",
  "description": "",
  "declared_phase": {"phase": "PLAN", "inferred": false, "confidence": null, "reasoning": null},
  "dependencies": [],
  "acceptance_criteria": ["a written implementation plan exists"],
  "required_artifacts": ["implementation_plan"],
  "executor_kind": null,
  "requires_operator_approval": false,
  "source_heading": "Milestone A: plan the change",
  "span": {"start": 291, "end": 417},
  "unknown_fields": {}
}
```

The span indexes the original text, so a compiler diagnostic points at the characters the author wrote.

## 3. The compiled plan

Compilation applies policy, injects the guardrails the runtime requires, and produces canonical JSON.
Milestone A as compiled, with the registry's contribution visible:

```json
{
  "stable_key": "a",
  "phase": "PLAN",
  "ordinal": 1,
  "dependencies": [],
  "acceptance_criteria": ["a written implementation plan exists"],
  "required_artifacts": ["implementation_plan"],
  "executor_kind": "plan.implementation",
  "tool_policy": {"permitted_tools": ["read_repository", "invoke_model"]},
  "approval_policy": {
    "required": false,
    "prompt": "Approve milestone a (plan the change) before plan.implementation proceeds."
  },
  "failure_policy": {"default_class": "NONRECOVERABLE", "max_attempts": 3, "blocks_phase": true},
  "source_provenance": {
    "design_doc_id": "acceptance_design_doc",
    "design_doc_revision_id": "ddr_01de479f46138c6a1730455d",
    "source_heading": "Milestone A: plan the change",
    "source_start": 291,
    "source_end": 417
  },
  "timeout_seconds": 1800,
  "phase_inferred": false
}
```

The document named no executor, so the compiler used the registered default for `PLAN` and copied that executor's tool policy, retry budget, and timeout into the plan.
Recompiling the same revision produces byte-identical canonical JSON and therefore the same `plan_hash`.
Changing any authority-bearing field, including an acceptance criterion, changes the hash.

## 4. Starting the WorkUnit

```bash
agent-ledger compile_design_doc docs/examples/work_unit_acceptance_design_doc.md
```

Returns the DesignDoc revision, the compiled plan revision, the plan hash, the diagnostics, and any execution blockers.

```bash
agent-ledger start_work_unit cpr_a0ce58277a14a9a9a4df3802
```

Creates the WorkUnit, its milestone executions, its first three events, and its enqueue outbox row in one transaction, then hands the root execution to DBOS under the workflow ID `work-unit:{work_unit_id}`.
Running the same command again returns the same WorkUnit with `created: false` and enqueues nothing.

Delivery is deliberately separate from creation, and it is honest about what happened.
With a resident DBOS runtime the command reports `delivered: true` and returns while the runtime executes.
Without one it reports:

```json
{"delivered": false, "durable": false, "reason": "no active DBOS runtime; the enqueue stays pending"}
```

The outbox row stays `PENDING` and any runtime that comes up can take it with `drain_work_unit_enqueues`.
A single-shot operator run can execute it in the foreground instead:

```bash
agent-ledger drain_work_unit_enqueues --inline
```

Foreground execution still needs the dispatch drainer running for agent-executed milestones, because those milestones submit dispatch intents and wait for them to settle.

## 5. The resulting event trace

This is the whole execution as the event log records it, including the operator wait in the middle.

```text
  1  WORK_UNIT_CREATED                -
  2  PLAN_BOUND                       -
  3  ROOT_WORKFLOW_ENQUEUED           -
  4  WORK_UNIT_STARTED                CLARIFY
  5  PHASE_SKIPPED                    CLARIFY
  6  PHASE_SKIPPED                    VALIDATE
  7  PHASE_STARTED                    PLAN
  8  MILESTONE_READY                  PLAN       a
  9  MILESTONE_STARTED                PLAN       a
 10  MILESTONE_SUCCEEDED              PLAN       a
 11  PHASE_COMPLETED                  PLAN
 12  PHASE_STARTED                    IMPLEMENT
 13  MILESTONE_READY                  IMPLEMENT  b
 14  MILESTONE_READY                  IMPLEMENT  c
 15  MILESTONE_STARTED                IMPLEMENT  c
 16  MILESTONE_STARTED                IMPLEMENT  b
 17  MILESTONE_SUCCEEDED              IMPLEMENT  c
 18  MILESTONE_SUCCEEDED              IMPLEMENT  b
 19  PHASE_COMPLETED                  IMPLEMENT
 20  PHASE_STARTED                    VERIFY
 21  MILESTONE_READY                  VERIFY     d
 22  MILESTONE_STARTED                VERIFY     d
 23  MILESTONE_SUCCEEDED              VERIFY     d
 24  PHASE_COMPLETED                  VERIFY
 25  PHASE_STARTED                    REVIEW
 26  MILESTONE_READY                  REVIEW     e
 27  APPROVAL_REQUESTED               REVIEW     e
 28  MILESTONE_WAITING_FOR_OPERATOR   REVIEW     e
 29  MILESTONE_BLOCKED                REVIEW     e
 30  PHASE_BLOCKED                    REVIEW
 31  WORK_UNIT_BLOCKED                REVIEW
 32  APPROVAL_RECEIVED                REVIEW     e
 33  MILESTONE_READY                  REVIEW     e
 34  WORK_UNIT_STARTED                -
 35  PHASE_STARTED                    REVIEW
 36  MILESTONE_STARTED                REVIEW     e
 37  MILESTONE_SUCCEEDED              REVIEW     e
 38  PHASE_COMPLETED                  REVIEW
 39  PHASE_STARTED                    DELIVER
 40  MILESTONE_READY                  DELIVER    f
 41  MILESTONE_STARTED                DELIVER    f
 42  MILESTONE_SUCCEEDED              DELIVER    f
 43  PHASE_COMPLETED                  DELIVER
 44  WORK_UNIT_SUCCEEDED              -
```

Read it as five facts.
`CLARIFY` and `VALIDATE` held no milestones and are `SKIPPED` rather than absent.
`b` and `c` interleave, because independent milestones in one phase run concurrently.
`d` starts only after both of them succeed.
Events 27 to 31 are the approval gate: the request is persisted, the milestone parks, the phase blocks, and the WorkUnit reports `BLOCKED` with a pending decision.
Events 32 to 44 are the resume after approval, and `f` runs only there.

## 6. Answering the decision

The cockpit shows the pending request; so does the CLI.

```bash
agent-ledger get_work_unit cddf8ddb3cf998ff2ac5937f40dc7111
```

```bash
agent-ledger submit_work_unit_decision cddf8ddb3cf998ff2ac5937f40dc7111 wud_3a748a11bf8fe4db92ff4d77 APPROVED idem-1 --decided-by rahul
```

The decision must name the request.
A decision for an unknown request, for another WorkUnit's request, or with an unrecognized value is rejected, and a second delivery of the same decision returns `applied: false` without writing a second event.

```bash
agent-ledger resume_work_unit cddf8ddb3cf998ff2ac5937f40dc7111
```

Resume returns blocked milestones to `READY` and re-drives the lifecycle.
Like `start_work_unit`, it hands the continuation to DBOS by default and reports when there is no runtime to take it; add `--inline` to drive it here.
Completed phases, completed siblings, artifacts, decisions, and history are untouched, and the root skips whatever already terminated.

## 7. What the cockpit shows

`GET /work-units/{id}` returns `work_unit_view.v1`: identity, DesignDoc revision, compiled plan revision and hash, root workflow ID, current phase, overall status, the status of all seven phases, milestone status by phase, the blocking condition, pending decisions, produced artifacts, recent domain events, and the DBOS workflow IDs for execution-level diagnostics.

The blocking condition is the operator's next action, in priority order: a pending decision outranks a blocked milestone, which outranks a failure.
Nothing in the view comes from a model transcript.

Supporting routes:

- `GET /work-units` and `GET /work-units?status=BLOCKED`
- `GET /work-units/{id}/events?after_sequence=0&limit=100`
- `GET /work-units/{id}/artifacts`
- `POST /work-units/{id}/decisions`
- `POST /work-units/{id}/cancel`
- `POST /work-units/{id}/resume`

There is deliberately no endpoint that sets a phase or a milestone status.

## 8. Operator commands, in one place

| Command | Effect |
| --- | --- |
| `compile_design_doc <path or ddr_id>` | ingest and compile, reporting diagnostics and blockers |
| `start_work_unit <cpr_id>` | create the WorkUnit and hand its root execution to DBOS |
| `get_work_unit <id>` | the full cockpit view |
| `list_work_units [--status]` | the summary list |
| `list_work_unit_events <id> [--after-sequence] [--limit]` | the append-only history |
| `list_work_unit_artifacts <id>` | produced evidence |
| `submit_work_unit_decision <id> <request_id> <APPROVED\|DENIED\|ANSWERED> <idempotency_key>` | answer one named decision |
| `cancel_work_unit <id> [--reason]` | cancel, propagating to live milestones |
| `resume_work_unit <id> [--inline]` | re-drive a blocked or waiting WorkUnit |
| `drain_work_unit_enqueues [--limit] [--inline]` | deliver pending root-workflow enqueues |
