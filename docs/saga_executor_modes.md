# Saga Executor Modes

`/saga` keeps `local_first_agent_os` as the source of truth. The executor backend only controls how the pow-wow work is simulated or run.

Default:

```bash
LOCAL_AGENT_SAGA_EXECUTOR=cli
```

This uses `CliPowWowExecutor`, the batch path.
It runs each tier's coding agent through its own headless CLI directly in the leased worktree, with no live-session layer in between.

CLI mode:

```bash
pi /saga --executor cli \
  --worktree-root /tmp/local-agent-saga-worktrees \
  "Use ai-business-portfolio reports and implement the next gated portfolio task"
```

Per tier:

- Senior tasks run `claude --print --output-format json` in the leased worktree.
- Staff tasks run `codex exec` (read-only sandbox for review) in the same worktree, so a reviewer sees the implementer's actual diff.
- Junior tasks bypass external CLIs entirely and run through the local delegate path (a bounded prompt to a local model, output captured as a ledger artifact, no worktree).

The executor preflights `codex login status` once per run and fails fast with a clear message when the codex token is missing or revoked.
Junior delegate tasks fan out concurrently, bounded by the junior bench capacity.
The per-task process timeout is `LOCAL_AGENT_SAGA_TASK_TIMEOUT_SECONDS` (default 3,600 seconds / one hour).

Credential note: when an `ANTHROPIC_API_KEY` is present in the environment, the claude CLI bills that key (per-token) instead of a Claude subscription.
To use the subscription, keep `ANTHROPIC_API_KEY` out of the environment the executor runs in.

Dry-run mode:

```bash
LOCAL_AGENT_SAGA_EXECUTOR=dry_run
```

This uses `DryRunPowWowExecutor`, creates saga/pow-wow/task/artifact ledger records, and does not allocate worktrees or run an external process.

Dev-only fake external process mode:

```bash
pi /saga --executor fake_process --worktree-root /tmp/local-agent-saga-worktrees "Use ai-business-portfolio reports and implement the next gated portfolio task"
```

This uses `FakeProcessPowWowExecutor`, allocates detached git worktrees under `LOCAL_AGENT_SAGA_WORKTREE_ROOT` or `~/.local-agent/worktrees/local_first_agent_os`, runs a deterministic fake process in each implementation-task worktree, captures command/cwd/stdout/stderr/exit code/diff summary/verification output, and persists those captures as ledger artifacts.

`LOCAL_AGENT_SAGA_EXECUTOR=fake_process` is also supported, but only as a pi-daemon startup override. Setting it on a short-lived client command such as:

```bash
LOCAL_AGENT_SAGA_EXECUTOR=fake_process pi /start /gemma4
```

does not change an already-running resident `pi-daemon`. For live operator dogfood, prefer the `/saga --executor fake_process` directive flag, or restart the daemon/launchd job with `LOCAL_AGENT_SAGA_EXECUTOR` set before it starts.

This mode is not Claude, Codex exec, local model agents, auto-merge, or real portfolio implementation.

## Review revision loop (senior <-> staff negotiation)

A code-kind pow-wow whose staff review verdict requests changes (first line contains BLOCK, REJECT, REQUEST_CHANGES, or CHANGES_REQUESTED) does not end there.
The executor runs a bounded negotiation in the same worktree: the implementer receives the review verbatim as a revision task, the reviewer re-reviews the new diff, and the loop repeats.

The loop ends on the first of:

- APPROVE (or any non-blocking verdict): the run completes normally and still ends at the CODE_MERGE gate.
- The hard round cap: `LOCAL_AGENT_SAGA_MAX_REVIEW_ROUNDS` (default 4).
- A convergence classifier judging the exchange unproductive: identical consecutive verdicts are circling by definition; otherwise the junior local model classifies the last two verdicts as PROGRESS, CIRCLING (style/naming nits, repeated findings), or ESCALATE (fundamental disagreement). Classifier failure defaults to PROGRESS because the cap already bounds the loop.

Every round persists its revision run, re-review verdict, and a `review_convergence.v1` artifact, so the ledger records how the pair converged.
Before any senior revision process starts, the executor durably records the blocking `review_result.v1` and a `bounded_revision_context.v1` envelope.
That envelope binds the exact base and blocked commits, retained branch/worktree, host-stamped reviewer provenance, complete-review artifact ID and digest, original revision scope, verification commands, and approval boundaries that remain in force.
The complete unrestricted reviewer prose stays in the referenced `review_result.v1`; it is not reduced to a closed `findings[]` taxonomy.
The senior revision prompt is reconstructed only from that typed envelope and its integrity-verified review reference.
An unresolved block appends a failed `<review_task>_unresolved` result, so the run (and its milestone) fails visibly instead of completing with a buried objection.

## Removed: Omnigent mode

The Omnigent live-session backend (`OmnigentPowWowExecutor`, the `omnigent` executor mode, and the `LOCAL_AGENT_SAGA_OMNIGENT_*` settings) was stripped from the active codebase.
The saga's implement/review flow is batch capture, so a live-session layer was pure overhead, and Omnigent's Pi harness was dead in Omnigent 0.4.0.
The archived implementation and revival notes live in `potential_directions/omnigent_live_backend/`.

## Task prompt and environment contract

Each agent task launch gets a first-message prompt built from the pow-wow task and context (role, saga goal, target project, startup-skill pointer, success criteria, dependency outputs, and code/advisory constraints).
Every typed senior and staff launch also receives the bounded `engineering_doctrine.v2` contract regardless of which target repository owns the work.
The host stamps its schema version and SHA-256 into the durable execution lease and run artifact.
Staff may BLOCK approval for a concrete doctrine violation, but must name the violated rule and specific code, contract, or invariant rather than treating a style preference as a violation.
The launched process also receives `LOCAL_AGENT_CONTEXT_JSON` (the whole `PowWowExecutionContext` payload, carrying the saga id, target project, and dispatch intent) and, for worktree launches, `LOCAL_AGENT_ASSIGNED_WORKTREE` in its environment; the per-field variables this sentence used to list are gone.
The executor captures command/cwd/stdout/stderr/exit code, changed files, diff summary, and verification output as ledger artifacts.

For planning-enabled code decompositions, model visibility follows the typed independent-before-junior contract documented in `docs/decomposition_dispatch.md`.
The senior and staff independent-reading turns receive the raw saga goal and repository but no dependency output.
The executor must receive ledger acknowledgement for each named `planning_evidence.v1` artifact before it schedules a dependent phase.
Junior verification planning is explicitly non-exhaustive, the senior owns the final plan, and the staff reviewer combines its independent reading with the reviewed implementation.

Claude and Codex are launched under the streaming execution supervisor, not a
Celery worker. One local asyncio supervisor owns one OS process group. It
normalizes JSONL output into append-only ledger events, heartbeats the execution
lease, polls cooperative cancellation, and sends SIGTERM followed by SIGKILL to
the whole process group when needed. A deadline is a recovery checkpoint, not a
provider-fallback trigger: the worktree, full transcript, binary patch, status,
and diff summary are preserved, and exactly one junior advisory review decides
whether to create bounded continuation intents or pause for an operator.

If the agent command exits successfully but verification commands fail, the executor result and pow-wow ledger status are `VERIFICATION_FAILED`, not clean `COMPLETED`. Missing commands or process failures stay `FAILED`. A run with no dispatchable implementation tasks records the pow-wow as `BLOCKED`.

## Branch-backed implementation checkpoints

Every code worktree is allocated on a unique `agent/<pow-wow>-<group>-<suffix>` branch rather than detached `HEAD`.
The implementation process may edit only that worktree, while staff review remains read-only in the same worktree and branch.
After an implementation command and all required verification commands succeed, the executor creates an `Agent checkpoint: <task>` commit if the worktree has uncommitted non-ephemeral changes.
If the implementation harness had already committed its work, the executor records that descendant commit instead.

The checkpoint requires the worktree to remain on its allocated branch and the allocation base to remain an ancestor of `HEAD`.
Failure of either invariant or of `git commit` fails the implementation task closed.
Removing the worktree removes only the checkout, not its `agent/...` branch, so the recorded commit remains available for review and later integration.
No agent branch is merged, rebased into the target, pushed, or deployed automatically.

## Durable ledger records

Every saga run writes durable truth to the coordination ledger (default root `~/.local-agent/coordination/local_first_agent_os`, override with `AGENT_COORDINATION_ROOT`):

- Task status: completed executor tasks are marked `COMPLETED` via `complete_task`; failed or blocked tasks are recorded via `fail_task` (status `PENDING` with an incremented retry count until `max_retries`, then `FAILED`). Failed work never stays silently `CLAIMED`.
- Approval gate: when a completed fake_process or cli run produces changed files in its worktree, the saga submits a `CODE_MERGE` approval request with status `PENDING`. Failed verification or checkpointing cannot create a merge request. Nothing merges until an operator resolves it; the request payload records the pow-wow id, executor status, target project, changed files, and the final branch/commit provenance for each checkpointed worktree.
- Artifacts: worktree allocation, external run captures (command, cwd, stdout, stderr, exit code, changed files, diff summary, verification output), a `worktree_commit_checkpoint.v1` for each verified implementation, a `code_patch.v2` per code worktree, and a `pow_wow_dispatch_summary`.
- The `code_patch.v2` artifact is the full reviewed diff from the allocation base to the final branch commit (scratch dirs excluded, 2 MB cap with a truncation flag), captured before worktree cleanup so an approved `CODE_MERGE` always has a patch fallback as well as a branch and exact commit SHA. Retrieve it with `get_artifact <artifact_id>` and apply with `git apply` in the target repo.

`dispatch_runner_result.v1` is a discriminated result contract rather than an
untyped bag. `result_origin` is `AUTOMATED` or `MANUAL_RECOVERY`, `result_state`
is one of the finite dispatch-result states, and `promotion_state` records the
post-review boundary. Legacy manual-recovery approval fields are normalized
into that same contract and fail closed unless branch/base/commit provenance
and a final staff `APPROVE` verdict are present.

Promotion follows the finite sequence `RESULT_RECORDED -> REVIEWED ->
MERGE_PENDING -> MERGE_APPROVED -> MERGED -> MILESTONE_COMPLETED`. Some normal
automated runs persist a later state directly because their implementation and
staff-review evidence are captured atomically, but no consumer may skip
`MERGE_APPROVED -> MERGED -> MILESTONE_COMPLETED`. `/approve-merge` still does
not merge code; its stdout and structured result print the exact approved
branch/commit and the required merge step. When the approval owns a milestone,
it also prints the milestone-completion step and the approved-GAWD command for
selecting the next dependency-ready milestone.

Inspect and resolve gates with the coordination CLI (run from the repo root):

```bash
uv run python agent_coordination_mcp.py --root "$HOME/.local-agent/coordination/local_first_agent_os" \
  list_approval_requests --status PENDING

uv run python agent_coordination_mcp.py --root "$HOME/.local-agent/coordination/local_first_agent_os" \
  resolve_approval_request <approval_id> approve --resolved-by rahul

uv run python agent_coordination_mcp.py --root "$HOME/.local-agent/coordination/local_first_agent_os" \
  list_tasks <pow_wow_id>

uv run python agent_coordination_mcp.py --root "$HOME/.local-agent/coordination/local_first_agent_os" \
  fail_task <task_id> "reason the task is blocked or failed"
```

Inspect a live frontier process or a recovery checkpoint with the same CLI:

```bash
ROOT="$HOME/.local-agent/coordination/local_first_agent_os"

uv run python agent_coordination_mcp.py --root "$ROOT" \
  list_execution_leases --status ACTIVE

uv run python agent_coordination_mcp.py --root "$ROOT" \
  list_execution_events <lease_id> --after-sequence 0 --limit 200

uv run python agent_coordination_mcp.py --root "$ROOT" \
  request_execution_cancel <lease_id> --reason "operator requested checkpoint" \
  --requested-by rahul

uv run python agent_coordination_mcp.py --root "$ROOT" \
  list_execution_checkpoints --status PENDING_JUNIOR

uv run python agent_coordination_mcp.py --root "$ROOT" \
  get_execution_checkpoint <checkpoint_id>
```

The junior review normally submits the checkpoint decision automatically. If it
fails closed to `PAUSED`, an operator may submit a validated
`checkpoint_review.v1` JSON object explicitly with
`decide_execution_checkpoint <checkpoint_id> --decision-json '<json>'`.

If an implementation commit survived but its staff process failed, enqueue only
the missing staff transition instead of repeating implementation:

```bash
uv run python agent_coordination_mcp.py request_recovery_staff_review \
  <checkpoint_id> \
  --target-project-id <project_id> \
  --branch <retained_branch> \
  --base-head-sha <40_character_base_sha> \
  --commit-sha <40_character_retained_commit_sha> \
  --milestone-id <milestone_id>

pi /dispatch
```

The request is idempotent for the exact checkpoint, branch, base, commit, and
milestone and rejects conflicting replays. The runner creates an isolated
worktree at that exact commit, validates the retained branch and ancestry, and
runs the configured staff reviewer read-only. The host stamps the review origin,
tier, harness, model, reasoning effort, execution lease, task, attempt, reviewed
commit, base, completion status, and exact engineering-doctrine version/hash into
`review_result.v1`. A merge approval requires that doctrine provenance to match
the contract currently owned by the control plane. A staff `BLOCK` first persists
the same `bounded_revision_context.v1` used by the normal review loop, including
the recovery checkpoint's original task contract, retained branch, and permission
envelope. It then starts the existing bounded senior-revision/staff-re-review loop;
an immediate `APPROVE` never starts the senior implementation harness. Any reviewer
filesystem mutation fails the review. A `CODE_MERGE` request is created only when
the final host-stamped staff verdict approves the same base and commit recorded by
the merge checkpoint.

If the staff process completed and wrote a host-stamped `review_result.v1`, but an
older host parser stored its explicit decision as `unclassified`, do not rerun
either model.
Recover the parser transition from the immutable dispatch result:

```bash
uv run python agent_coordination_mcp.py recover_unparsed_staff_review \
  <failed_dispatch_intent_id>
```

The command accepts only a failed code dispatch at `RESULT_RECORDED`, an exact
retained checkpoint, and an `unclassified` staff result whose complete provenance
matches that checkpoint and whose original text parses as `APPROVE` under the
current typed parser.
It preserves the original review, appends a digest-bound recovered decision, and
opens the ordinary `CODE_MERGE` gate idempotently.
It does not approve or merge anything.

After an operator approves that exact commit and the project's integrated branch
contains it, a WorkUnit-owned dispatch can discharge its blocked milestone from
the same evidence:

```bash
uv run python agent_coordination_mcp.py adopt_recovered_work_unit_dispatch \
  <failed_dispatch_intent_id>
uv run python agent_coordination_mcp.py resume_work_unit <work_unit_id>
```

Adoption refuses before integration, on revoked or missing approval, on any
target/base/commit mismatch, or when the recovered result cannot produce every
artifact the compiled milestone requires.
It records a fresh recovery attempt through the normal lifecycle transitions
rather than rewriting either terminal dispatch history or the failed attempt.

Optional OTLP export accepts authenticated collector headers through
`LOCAL_AGENT_OTEL_TRACES_HEADERS`, encoded as a JSON object. Telemetry is not
part of execution correctness; disabling or losing it cannot change ledger,
checkpoint, continuation, or approval state.

The saga directive result surfaces the same records: `operator_summary.merge_approval_id`, `operator_summary.merge_approval_status`, and the full `merge_approval_request` payload, plus a `merge_approval:` line in the operator report.
