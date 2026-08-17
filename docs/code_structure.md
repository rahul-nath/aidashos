# Control-plane code structure

The control plane keeps its public CLI, but the CLI is now a serialization adapter rather than the in-process API.

The `coordination`, `pow_wow`, and `workflow` directories are namespace packages whose `__init__.py` files expose the intended public surface while implementation modules retain lifecycle-specific names.

## Coordination protocol

`coordination/contracts.py` owns the closed command vocabulary, flag vocabulary, typed command variants, and result sum type.

Enums alone are not a useful command sum because an enum cannot carry the required fields of `SubmitArtifact`, `OpenExecutionLease`, or `CompleteDispatchIntent`.

The command protocol is therefore a union of frozen data variants, with enums used as discriminants and for finite fields such as terminal status and approval decision.

`coordination/transport.py` is the only module that lowers a typed command to argv, launches the compatibility CLI, handles large-payload file transport, and parses JSON.

`CoordinationTransportFactory.create()` has the useful type `Settings -> CoordinationTransport`.

`RecordingCoordinationTransport` is the deterministic test provider.

The legacy list adapter remains for cross-agent CLI integration tests and the serialized DBOS command boundary, but it validates the command name against `CoordinationCommandName` before launching a process.

Production orchestration callers construct command variants directly.

Known response envelopes are parsed into `EntityResult`, `CollectionResult`, or `AcknowledgementResult` before executor callbacks consume them.

Ledger operations are grouped by the durable state they own:

- `coordination/store.py` owns schema, connections, paths, transactions, and event serialization.
- `coordination/collaboration.py` owns sessions, file claims, notes, and handoffs.
- `coordination/projects.py` owns GAWD documents and saga lifecycle.
- `coordination/milestones.py` owns milestone transitions and evidence.
- `coordination/pow_wows.py` owns pow-wows, tasks, artifacts, delegation, permissions, and evaluation.
- `coordination/approvals.py` owns approval requests and decisions.
- `coordination/dispatch.py` owns dispatch intent transitions.
- `coordination/execution.py` owns execution leases, durable events, and retention.
- `coordination/durable.py` owns DBOS serialization boundaries.
- `coordination/saga_coordinator.py` owns the high-level staged saga runner.
- `coordination/cli.py` owns argparse and MCP serialization only.

## Pow-wow execution

`pow_wow/types.py` is the shared data hub.

Git and branch provenance live in `pow_wow/git_ops.py`, prompt projection lives in `pow_wow/prompts.py`, process capture lives in `pow_wow/process.py`, and review parsing lives in `pow_wow/protocol.py` and `pow_wow/review.py`.

`TaskPurpose` is parsed once when a legacy task payload is constructed.

Executor control flow then matches the enum instead of sniffing role and task-name strings repeatedly.

`ReviewVerdict.parse()` converts the first reviewer decision line into `ReviewDisposition` once.

The revision loop consumes `ReviewDisposition.requests_changes` rather than maintaining its own token vocabulary.

## ViewBlock

A `ViewBlock` is a sum type describing how faithfully a prompt block represents its durable source.

The variants are `VerbatimViewBlock`, `CompactedViewBlock`, and `TruncatedViewBlock`.

The compacted and truncated variants must carry a source label and the original size.

This starts fixing model-output-shaped control flow because prompt loss is now represented in data rather than hidden in string slicing.

The model still emits text, but the executor can distinguish exact evidence from a derived view before that text reaches a reviewer or classifier.

The ledger artifact remains unchanged, so a later policy can re-derive another view without treating a lossy prompt as durable truth.

## Workflow facade

`WorkflowEngine` remains the public facade used by the runtime and tests.

Its implementations are grouped into knowledge, browser, model, workspace, and saga mixins.

The mixins share a small typed cross-domain contract in `workflow/base.py` instead of relying on arbitrary attributes or a generic utility module.

Shared operations are grouped separately as workflow core, saga support, and compaction support.
