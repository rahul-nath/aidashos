# Agent privileges

The operator's own statement of who may do what.

This file is the authority. It is not documentation of the code; the code reads
it. `policy_document.py` parses it into an immutable, content-hashed revision,
and `capability_gate` consults that revision before any agent process is spawned.

## How it is read

Each `## Principal:` section names an agent and what it may and may not do.

- `May:` is an **allowlist**. When a principal declares one, anything not on it
  is refused, whatever the compiled plan permitted.
- `Never:` is an **absolute denial**. It outranks the compiled plan *and* any
  grant in `tool_permission_requests`. This is the line that cannot be undone by
  an agent asking nicely at runtime.
- A principal with no section here is governed by its compiled plan alone, and by
  `## Principal: default` if one is present.

A principal's own section replaces the default section; the two are not merged.
And one capability may not appear on both of a principal's lines. That is a
compile error rather than a precedence rule, because resolving it either way
would carry out an instruction nobody wrote, and resolving it to the denial
silently would leave a `May:` line that does nothing with no sign that it does
nothing.

## What you may write on a line

Either vocabulary, and prefer the first.

**Operator actions** are what a permission envelope authorises, and what a person
actually decides about: `deploy`, `dependency_install`, `merge_to_main`,
`code_worktree_write`, `test_command_execution`, `network_access`,
`external_communications`, `spend_money`, `secret_or_credential_access`,
`destructive_file_operations`, `read_repo_context`, `write_ledger_artifacts`,
`run_local_model_delegates`, `request_operator_decisions`.

**Capabilities** are what the runtime enforces: `write_repository`,
`run_command`, `publish_deployment`, `read_repository`, `invoke_model`,
`write_artifact`, `ask_operator`, and the rest of `Capability`.

`work_units/permissions.py` owns the translation, so an action written here means
exactly what it means in a permission envelope, and there is one place to change
if that ever moves.

One action can mean several capabilities: `dependency_install` is a command *and*
network egress. So two lines can collide without looking like they collide -
`May: test_command_execution` against `Never: dependency_install` both name
`run_command`. When that happens the error names the actions you wrote, not just
the capability underneath, and you resolve it by naming the capability directly
on whichever line you meant.

A name that is neither an action nor a capability is a compile error. So is an
action that needs no runtime capability, like `prepare_isolated_worktrees`: a
rule that governs nothing reads as protection and is not.

## How it is protected

There is no portable way to make a file on this machine writable by one person
and not another, so this does not pretend to. What it does instead is make an
edit *impossible to miss*: the parsed content is hashed, the hash is pinned in
`policy_document.POLICY_CONTENT_HASH`, and
`test_the_pinned_policy_hash_matches_the_document_on_disk` fails until somebody
updates it in the same change.

That is a tripwire, not a lock. It cannot tell whether an edit was authorised; it
refuses to let one pass unnoticed, which is the same bargain
`SCHEMA_CONTENT_HASH` makes for the database schema and is the strongest honest
guarantee a local-first system can offer.

Approved by: rahul
Reviewed on: 2026-08-05

## Principal: default

Applies to any agent with no section of its own.

The six here are the ones no compiled plan should be able to authorise without a
person in the loop, and they are denied to every agent before any one of them is
named.

Never: deploy, merge_to_main, spend_money, external_communications, secret_or_credential_access, destructive_file_operations

These sections are keyed on the **seat**, not on the vendor sitting in it.
A grant belongs to the seat: an implementer writes code and runs commands because it is the implementer, and the same vendor reviewing must not.
`configs/staffing.toml` decides which harness holds which seat, and that swap is now a one-line edit there with nothing to change here.

Keying them on the vendor said the same thing only by coincidence, and the coincidence had to be maintained by hand.
A staffing swap meant editing this file, re-pinning its hash, and correcting prose in two other files, or else the gate denied the implementer the write its own compiled plan had granted.
That is a static rule breaking a modular seat, and it cost a demo more than once before it was fixed on 2026-08-11.

`capability_gate.policy_principal` resolves a caller to its seat: by the role the compiled plan gave it, then by the seat the dispatch path declares from that plan, and only then by the bench, provided the vendor holds exactly one.
A vendor may hold two seats at once, which is what an outage staffing looks like, so the role and the declared seat are asked before the bench.
The declared seat exists because a plan role like `implementer` names no seat by itself, and the bench keys on the vendor - under an outage pairing that answered with the reviewer's seat and denied a live implementation milestone `run_command` on 2026-08-29.
A section named for a vendor still works and is matched last, so one may be pinned deliberately without giving up the seat sections.

## Principal: senior

The implementation seat. It writes code and runs commands in an isolated
worktree, and it never ships.

May: read_repo_context, code_worktree_write, test_command_execution, run_local_model_delegates, write_ledger_artifacts, request_operator_decisions
Never: deploy, merge_to_main, spend_money, external_communications, secret_or_credential_access, destructive_file_operations

## Principal: staff

The review seat. A reviewer inspects and must not mutate what it is reviewing;
the read-only sandbox enforces that at the process level and this says the same
thing where an operator can read it.

May: read_repo_context, run_local_model_delegates, request_operator_decisions
Never: code_worktree_write, test_command_execution, deploy, merge_to_main, spend_money, external_communications, secret_or_credential_access, destructive_file_operations

## Principal: junior

The local seat. It answers from the served local model and has no worktree, so
it has nothing to write and nothing to run.

May: read_repo_context, run_local_model_delegates, write_ledger_artifacts, request_operator_decisions
Never: code_worktree_write, test_command_execution, deploy, merge_to_main, spend_money, external_communications, secret_or_credential_access, destructive_file_operations
