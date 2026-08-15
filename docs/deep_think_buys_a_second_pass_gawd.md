# THE GAWD DOC - Mini

**Draft ID:** deep-think-second-pass
**Project:** local_first_agent_os | **Version:** deep-think-v1 | **Status:** DRAFT (operator review required) | **Date:** 2026-08-14

**Priority order:** correctness > stability > debuggability > throughput > latency

## 0. Current State Verification - 2026-08-14

Every claim here was read off this checkout today, not recalled.

Every text model this system serves locally runs with native reasoning off as a matter of policy.
`configs/model_registry.toml` sets `reasoning = "off"` on gemma4, on the qwen3.8 fallback, and on glimmer, and each entry carries a measured justification: on one bounded classification gemma4 spent 938 tokens in 87.6 seconds thinking against 46 tokens in 4.9 seconds not, and the fast answer was the right one; glimmer paid a 2.3x latency tax for an answer that did not change.
The registry's own comment states the policy this document builds on: tasks that deserve deliberation ask for it per call rather than every caller paying for it by default.
Nothing in the delegate lane implements that per-call ask today.

The junior lane is one bounded generation.
`delegate_agent_task` in `src/local_first_agent_os/delegation.py` builds one `AgentTask` with `max_tokens=2048` by default, routes it through one adapter call, and returns one `AgentResult`.
`src/local_first_agent_os/local_delegate.py` exposes that call to executors as the single `DelegateFn` seam and records every call in `model_invocations` against a registered `workflow_runs` row.
No call site issues a second pass, and no prompt carries a prior pass's output forward.

Per-call native thinking is representable but is not a lane behavior.
`contracts.py` knows the `enable_thinking` and `reasoning_strength` chat-template dialects, and the glimmer entry declares `reasoning_dialect = "reasoning_strength"` so that its `reasoning = "off"` actually lands.
That machinery decides what one request asks the chat template for; it does not give any caller a second sequential generation.
The registry has also already paid for thinking-shaped output once: the chandra-ocr-2 entry pins `reasoning_format = "none"` because llama.cpp's default parsing routed everything inside its `<think>` tag into `reasoning_content` and handed back an empty `content`.

No deep-think flag exists.
`src/local_first_agent_os/settings.py` has no field for it, and `docs/configuration.md`, which is generated from the settings model by `scripts/dump_config_reference.py` and never edited by hand, lists no such flag.

## 1. Theory of the System

There are two different scratchpads a model can think on, and this system currently offers its local models neither.

Native reasoning is hidden tokens the model emits before its answer inside one generation.
This system turns it off by policy, for measured reasons, and thinking-shaped output is also awkward to consume: the registry already pins `reasoning_format` on one model because the default parsing returned its whole answer as reasoning and its `content` empty.

The second scratchpad is explicit text the model is asked to write before answering, carried back to it as context, and answered from in a second generation.
The mechanism is two sequential calls: pass one elicits a written working analysis, the harness stores it and carries it into the prompt for pass two, and pass two answers.
The value is the generation boundary, not any tool syntax.
A caller of a vendor API buys this boundary by defining a near-no-op tool such as `deep_think` and letting the model emit its scratchpad as the tool-call argument; this system is the harness, so it can simply make the second call itself.

That is test-time compute at the orchestration layer.
The model gets more sequential token-generation steps to transform the problem, even though native reasoning stays off.
And it is honest about what it is: the scratchpad is ordinary visible output the model was instructed to produce, not the model's hidden reasoning recovered, and the record must never claim otherwise.

## 2. Why This Exists

The junior lane is asked bounded questions, and some of them deserve more than one bounded generation.

The registry policy is right: always-on thinking spends roughly twenty times the tokens to make easy calls worse.
But the policy's own escape hatch, asking for deliberation per call, has no implementation, so today every delegate call is a single pass no matter what the task deserves.
Flipping native reasoning back on per call is the other road, and it is worse on this system's own terms: it is dialect-specific per chat template, it pays the measured latency tax inside one opaque generation, and the deliberation it buys is invisible to the ledger.

A second explicit pass has the property this system specifically values: the scratchpad is durable, legible evidence.
Model calls here are recorded; a scratchpad that is ordinary output text is recorded the same way, read by an operator, and cited by a reviewer.
Hidden reasoning tokens could never be that, even where the template supports them.

## 3. Happy Path / Golden Flow

1. A caller marks one delegate task as deserving deliberation, and the flag is on.
2. Pass one sends the task with an instruction to write a working analysis first, and nothing else.
3. The harness records the scratchpad as a task artifact labeled as elicited scratchpad text.
4. Pass two sends the same task with the scratchpad in context and asks for the answer.
5. The answer comes back, and both passes sit in `model_invocations` against the same workflow row.
6. With the flag off, or the task unmarked, the lane behaves exactly as it does today: one call, one result.

## 4. This Version - Scope & Non-Goals

In scope: one feature flag, the two-pass path behind it at the delegate seam, the scratchpad artifact, and the proof that off means unchanged.

Out of scope, deliberately:

- No interpreter loop. A tool-use loop the OS drives is its own design and its own document; this version buys exactly one extra generation and parses no tool grammar.
- No change to native reasoning policy. The registry's `reasoning = "off"` entries and the dialect machinery in `contracts.py` stay as they are.
- No claim of parity with native high reasoning. Reasoning tokens are trained for that job; an elicited scratchpad is not, and this document does not assume it competes.
- No frontier-lane change. The `claude` and `codex` CLIs own their own loops; this is for the models this repository calls itself.

## 5. Core Design

The flag is one settings field, following the marker convention the configuration reference derives from.
`deep_think_second_pass: bool = False` in `src/local_first_agent_os/settings.py`, carrying `json_schema_extra={"feature_flag": True}` and a description that says what turning it on changes; the environment spelling is `LOCAL_AGENT_DEEP_THINK_SECOND_PASS`.

The second pass lives at the delegate seam, not inside any adapter.
`delegate_agent_task` is the one place every junior call already passes through, so the two-pass shape is written once there and every harness that delegates, Pi directives and the resident dispatcher alike, gets it identically.
Which model answers stays behind the adapter registry exactly as it does today.

The scratchpad is bounded, carried, and recorded.
Pass one runs under its own explicit token bound; its output is stored as a task artifact whose name and metadata say it is elicited scratchpad text, and it joins pass two's prompt verbatim.
The record says what the text is, output the model was instructed to write, because a record that called it recovered hidden reasoning would be false.

## 6. The Failure That Matters Most

The flag is on paper a kill switch, and the off-path behavior drifts anyway.

This feature ships behind a default-off flag precisely so that shipping it changes nothing.
If the change that introduces the branch alters the single-pass path, a changed prompt, a changed invocation record, or a changed result shape, then the flag gates nothing and the kill switch is a lie.
The off path must be provably the today path, and that proof is a test, not a reading.

The second failure worth naming: a pass-one failure must fail the task visibly rather than silently degrade to single-pass.
An operator who asked for deliberation and got a confident single-pass answer instead has been lied to by omission.

## 7. Verification

With the flag off, the delegate lane produces a byte-identical prompt, one recorded invocation per task, and no scratchpad artifact.
With the flag on and a task marked, two invocations are recorded against the same workflow row, the pass-two prompt contains the pass-one scratchpad, and the scratchpad artifact exists and is labeled as elicited text.
With the flag on and pass one failing, the task fails with the pass-one error rather than answering single-pass.

## 8. Execution Milestones

### Milestone 0: Decide the contract of the second pass

Phase: PLAN
Description: Settle, before any code changes, the exact shape of the feature: the flag's description text, how a caller marks one task as deserving deliberation, the pass-one instruction and its token bound, the artifact name and metadata that label the scratchpad as elicited text, and what a pass-one failure does. Read the registry's reasoning policy and the delegate seam first; the design must not move native reasoning policy and must not touch any adapter.
Acceptance: the plan names the flag, its default, and the caller-facing marking for a deliberate task
Acceptance: the plan states the pass-one instruction, the scratchpad token bound, and the artifact name and labeling
Acceptance: the plan states that a pass-one failure fails the task and why silent single-pass degradation is refused
Artifacts: implementation_plan

### Milestone 1: The flag, and the reference that derives from it

Phase: IMPLEMENT
Depends on: 0
Executor: implement.code_change
Description: Add `deep_think_second_pass` to `src/local_first_agent_os/settings.py` as a boolean defaulting to false, carrying the `feature_flag` marker in `json_schema_extra` and a description of what it changes, then regenerate `docs/configuration.md` with `scripts/dump_config_reference.py` rather than editing it by hand. Nothing reads the flag yet; this milestone is the switch existing before the machine it controls.
Acceptance: the settings field exists, defaults to false, and carries the feature_flag marker
Acceptance: docs/configuration.md lists the flag and was regenerated by the script, not hand-edited
Acceptance: the full repository gate passes with the flag present and off
Artifacts: source_patch

### Milestone 2: The two-pass path behind the flag

Phase: IMPLEMENT
Depends on: 0, 1
Executor: implement.code_change
Description: Implement the second pass at the `delegate_agent_task` seam in `src/local_first_agent_os/delegation.py`. With the flag off or the task unmarked, the existing single-call path runs unchanged. With the flag on and a task marked, pass one elicits a bounded working analysis, the harness records it as a task artifact labeled as elicited scratchpad text, and pass two answers with the scratchpad in its prompt; both calls record in `model_invocations` against the same workflow row through the existing seam. A pass-one failure fails the task with pass one's error.
Acceptance: flag off or task unmarked produces one recorded call and an unchanged prompt
Acceptance: flag on and task marked produces two recorded calls against one workflow row
Acceptance: the pass-two prompt contains the pass-one scratchpad verbatim
Acceptance: the scratchpad artifact exists and its label says it is elicited output, not hidden reasoning
Acceptance: a pass-one failure fails the task with that error rather than degrading to single-pass
Artifacts: source_patch

### Milestone 3: Prove off is today and on is two passes

Phase: VERIFY
Depends on: 1, 2
Executor: verify.tests
Description: Cover the three claims in section 7 with tests at the delegate seam, using the adapter fakes the suite already has. The off-path identity test is the one that matters most: it is the claim that makes the flag a kill switch, so it asserts prompt bytes, call count, and artifact absence rather than a summary of them.
Acceptance: a test proves the off path is byte-identical in prompt, single in recorded calls, and empty of scratchpad artifacts
Acceptance: a test proves the on path records two calls, carries the scratchpad into pass two, and stores the labeled artifact
Acceptance: a test proves a pass-one failure fails the task with the pass-one error
Acceptance: the full repository gate passes
Artifacts: test_result

### Milestone 4: Operator review and merge approval

Phase: REVIEW
Depends on: 3
Executor: review.operator
Approval: required
Description: The operator reads the diff and the evidence and approves the exact commit. The question worth asking is about the off path: not whether the feature works, but whether anything about today's single-pass lane moved while the switch was being installed.
Acceptance: an operator approved the exact commit having read the off-path identity evidence
Artifacts: operator_approval

### Milestone 5: Record what the switch buys and costs

Phase: DELIVER
Depends on: 4
Executor: deliver.artifact
Description: Write down how to turn the second pass on, what one deliberate task costs against a single-pass task, where the scratchpad artifacts land and how an operator reads one, and that the kill switch is the default-off flag. The record repeats the honesty constraint so nobody downstream re-labels the scratchpad: it is text the model was told to write, not recovered hidden reasoning.
Acceptance: the record states how to enable the feature and what it costs per deliberate task
Acceptance: the record states where scratchpad artifacts land and how an operator reads one
Acceptance: the record states the kill switch and repeats what the scratchpad is and is not
Artifacts: delivery_record

## 9. Operational Contract

**Service levels.**
- A deliberate task costs two model calls; nothing else in the lane may get slower.

**Input bounds.**
- Pass one runs under its own explicit token bound, and the scratchpad enters pass two verbatim within it.

**Idempotency / replay.**
- Both passes record in `model_invocations` like any other call; a replayed deliberate task replays as two calls.

**Observability.**
- The scratchpad is a recorded artifact labeled as elicited output, so one place answers what the model was thinking on.

## 10. Rollout / Migration / Rollback

The flag ships default off, so rollout is the merge, and enablement is an operator decision per environment.
Rollback is turning the flag off, which returns the lane to the single-pass path the off-path identity test proves unchanged.

## 11. Risk Synthesis / Known Limitations

An elicited scratchpad is not native reasoning, and this document does not claim it competes with a template's own thinking at full strength.
The second pass doubles cost and latency for every task marked deliberate, which is why marking is per task and never blanket.
The scratchpad is model output and can be wrong; recording it makes the deliberation legible, not correct.
What this removes is the case where a task deserved a second thought and the lane had no way to buy one.
