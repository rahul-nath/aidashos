# Demo shooting script: one governed WorkUnit, start to merge approval

A recording plan for a 6 to 8 minute video, written so the recording is narration over commands that already work.

Read [cockpit_e2e_runbook.md](cockpit_e2e_runbook.md) first; this is that runbook arranged for a camera, with the parts that make bad television removed.
Every command here is copy-paste and appears in the order you run it.

## What has to land before you can shoot this

The plan is to **drive the run from the cockpit** and say the equivalent command aloud as each control is pressed.
Part of that does not exist yet, and this section is here so the gap is found now rather than on shoot day.

Landed and usable today: `POST /authoring/design-docs/compile`, `POST /work-units`, the WorkUnit list, detail, events and artifacts views, the Approve and Deny buttons, `POST /work-units/{id}/cancel` and `/resume` on the server, and one runtime button that runs the dispatcher.

Not landed, and needed for a cockpit-driven shoot, all of it scoped in [cockpit_routes_and_agent_acl_gawd.md](cockpit_routes_and_agent_acl_gawd.md):

- `POST /work-units/drain`. With the resident drainer stopped, which is what the next section recommends, **nothing moves a QUEUED WorkUnit without this route or a terminal command.**
- Cockpit controls for compile, start, and drain. The two write routes above are reachable today only by `curl`; there is no button.
- Cockpit controls for cancel and resume. Both routes exist and the frontend has never called either.
- The equivalent-command line beside each control, which is the narration device this shoot is built on.

Until those land, everything below still shoots, with the terminal driving and the cockpit watching.
That is a good video. The cockpit-driven version is a better one, and it is four milestones of work rather than a rewrite.

## Before you record

Do this the day before, not on camera. It is the part that takes minutes and shows nothing.

```bash
./scripts/first-run-check.sh --probe-frontier-models
```

Everything must read `ok`. The flag makes each staffed frontier model answer a one-line completion; a model id that quietly stopped existing is exactly the failure you do not want to meet mid-recording.
Then put the two operator commands on `PATH`, because every shot below types them bare:

```bash
./scripts/install_pi_shell.sh
```

That is `uv tool install -e .`, so it installs every `[project.scripts]` entry: `pi`, which drives the workflows, and `agent-ledger`, which reads the coordination ledger.
A viewer who runs this one line can reproduce every command in this script verbatim, which is the reason the commands are short rather than a `uv run python ...` line that would not fit on screen.

Then pre-warm the slow things, because a viewer will not wait for them.
This is **two** scripts, not one. `start-agent-runtime.sh` does not start the API or the web client:

```bash
./scripts/start-agent-runtime.sh   # compose, daemons, resident loops, preloads gemma4, ~40s
```

```bash
./scripts/start-ui-stack.sh        # FastAPI on :8000 and the cockpit on :5173, leave running
```

## Decide the drainer before you record, because it decides the whole shape

`start-agent-runtime.sh` starts two resident loops, and on this machine both are supervised by launchd and running right now:
`com.rahul.local-first-agent.enqueue-drainer` polls every 5 seconds and hands a QUEUED WorkUnit to DBOS, and `com.rahul.local-first-agent.ledger-dispatcher` polls every 2 seconds and claims dispatch intents.

**With them running, the demo drives itself.** You press Start and the work is claimed before you finish the sentence.
That is the honest production configuration and it is bad television, because the system's most interesting property is that each step is a separate, auditable decision.

**Recommended: stop both loops for the shoot** and move the work by hand, so every transition on camera is one you caused.

```bash
launchctl bootout gui/$(id -u)/com.rahul.local-first-agent.enqueue-drainer
```

```bash
launchctl bootout gui/$(id -u)/com.rahul.local-first-agent.ledger-dispatcher
```

Put them back afterwards, or the machine stops draining its own queue and you will not notice for a day:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rahul.local-first-agent.enqueue-drainer.plist
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rahul.local-first-agent.ledger-dispatcher.plist
```

Say on camera that you turned the loops off to make the steps visible, and that in normal operation they run and the queue drains itself.
That sentence costs five seconds and it prevents the obvious objection, which is that a system needing a human at every step is not an autonomous one.

## The document this shoot uses

**Reserved: [`docs/publish_deployment_refusal.md`](publish_deployment_refusal.md).**
Do not run this document outside the shoot, and do not let another session start it, or the demo loses its unstarted subject.

It was picked over the other candidates for four reasons.
It is 72 lines, so it fits on screen and a viewer can read it while you talk.
It has the full five-milestone arc: PLAN, IMPLEMENT, VERIFY, REVIEW with `Approval: required`, and DELIVER, so every shot below has something real behind it.
It carries a `Target project:` line already, which is the preamble the three sparse drafts each needed before they would compile.
And it is genuinely not started, so the run is real work rather than a re-enactment.

It also happens to be *about* refusal: the change makes `PUBLISH_DEPLOYMENT` fail loudly instead of being silently removed by intersection.
A video whose argument is "this system refuses visibly" running a work item that makes a refusal visible is worth one sentence of narration.

Compiled clean on 2026-08-15 against the live ledger:

```bash
uv run python -m local_first_agent_os.coordination.cli compile_design_doc docs/publish_deployment_refusal.md
```

`"validation_status": "VALID"`, `"runnable": true`, zero execution blockers, zero diagnostics, plan hash `d0ded5251cd2856f950ddceea9d173d775a4a5634de39f6e2afe964339a6bdf5`.

Two cautions on it.
Its target project is this repository, so the senior agent works in a worktree of `local_first_agent_os` itself; that is what makes the `git worktree list` shot at 3:30 real, and it is also why the document's constraints forbid touching generated files.
And the ledger already holds **two CANCELLED work units** titled "publish_deployment refuses loudly", so confirm a fresh start is accepted during the rehearsal rather than discovering the identity check on camera.

Pick a target project with a fast verification command if you swap the document.
The demo is about the pipeline, not about somebody's test suite, and a two-minute `pytest` is two minutes of nothing on screen.

Decide in advance whether the senior implementation step runs live.
It spends real frontier quota and takes several minutes.
Both options are scripted below; **pre-recorded is the better video** and is not dishonest as long as you say so.

**Terminal setup:** two windows, side by side, large font. Left is the operator terminal. Right tails the ledger. Browser on a third of the screen or on a second display.

Start the right-hand window with this and leave it running the whole time; it is the shot that makes the system legible:

```bash
watch -n 2 'agent-ledger list_dispatch_intents | head -40'
```

---

## 0:00 - 0:40 | The claim

**On screen:** the README's opening, then `./scripts/first-run-check.sh` output.

**Say:** this runs agent work unattended and leaves an audit trail. Everything is local: the ledger is Postgres on this machine, the junior model is llama.cpp on this machine, and the frontier agents run under my own subscriptions through their own CLIs. Nothing here calls an API I do not control.

**Why this shot:** the check output is the fastest honest proof that a real machine is configured, and it names every dependency without a slide.

---

## 0:40 - 1:30 | The document is the unit of work

**On screen:** [`docs/publish_deployment_refusal.md`](publish_deployment_refusal.md) open in an editor. Scroll slowly through milestones, non-goals, and acceptance criteria.
At 72 lines the whole document fits in two screens, which is the reason it was reserved for this shoot.

**Say:** work starts from a document, not a prompt. It declares milestones, what is out of scope, how each milestone is verified, and what the agent is permitted to do. That last part becomes an enforced permission envelope, not a suggestion in a prompt.

**Then compile it.** One command, which is the one to type on camera:

```bash
agent-ledger compile_design_doc docs/publish_deployment_refusal.md
```

Verified 2026-08-15 against the live ledger. It returns a `design_doc_revision_id`, a `compiled_plan_revision_id`, a `plan_hash`, `"validation_status": "VALID"`, `"runnable": true`, and an empty `execution_blockers`.

Once the cockpit has a Compile control, press it instead and read the equivalent command off the screen.
The same operation over HTTP, if you want to show that the route and the command are one thing:

```bash
curl -sS -X POST http://127.0.0.1:8000/authoring/design-docs/compile -H 'content-type: application/json' --data-binary @/tmp/compile.json | python3 -m json.tool
```

**Say:** compiling validates it and produces a hashed plan revision. If the document is ambiguous, this fails here with diagnostics, before anything runs. The hash is what later provenance is stamped against, so the plan that executes is provably the plan that was approved.

**Why this shot:** it distinguishes the system from a prompt runner in one command, and a compile failure is a better demo than a success if you have one handy.

---

## 1:30 - 2:30 | Start it, and watch the ledger

Start the WorkUnit from the plan revision the compile step just printed, and bind it to the exact hash so the thing that runs is the thing that was approved:

```bash
agent-ledger start_work_unit <compiled_plan_revision_id> --approved-plan-hash <plan_hash> --title "publish_deployment refuses loudly"
```

**On screen:** the terminal, then cut to the right-hand ledger window as the WorkUnit appears at QUEUED.

**Say:** that wrote the WorkUnit and its enqueue row in one transaction. It is a row in Postgres, which is the whole point: if this machine loses power right now, the work is not lost, because nothing important is in a context window.

**Stay on the WorkUnit lane for the whole video.** The compile step above produced a plan revision, and `start_work_unit` is what consumes one.
`pi /start /new-project` is the *other* execution path, the saga intake lane, and mixing the two on camera is the fastest way to confuse a viewer who is trying to learn one model.

Now nothing happens, because you stopped the drainer. That pause is the next shot, not a mistake:

```bash
agent-ledger drain_work_unit_enqueues --limit 1
```

**Say:** a queued WorkUnit is handed to the durable runtime by a drainer. Normally that is a resident loop polling every five seconds. I turned it off so you can see that the handoff is a real step with a real record, and not something that happens invisibly.

**Then show the cockpit.** Browser to `http://127.0.0.1:5173`, WorkUnits list, click through to detail.
The cockpit is the Vite dev server on **:5173**; :8000 is the FastAPI application it reads from.

**Say:** the cockpit reads the same ledger. Every control on it names the exact operator command it runs, so nothing here is a button that does something you could not have typed.

**This is the shot that depends on unbuilt work.** See "What has to land before you can shoot this" at the top.
Until the cockpit controls exist, this shot is the read-only tour it has always been, and the driving happens in the left-hand terminal.

---

## 2:30 - 3:30 | The gate that fails closed

**On screen:** the approval request in the cockpit, still pending.

**Say:** nothing has run yet. The plan is compiled and the work is queued, and it stops here until a person approves it. This is the property the whole system is built around: merges, deploys, spend, secrets, and outbound messages all fail closed behind a gate like this one.

```bash
pi /approve-most-recent
```

**Why this shot:** most agent demos show speed. This one shows refusal, which is the harder thing to build and the reason anyone would trust it overnight.

---

## 3:30 - 5:30 | The agent works, in an isolated worktree

**If running live:**

```bash
pi /dispatch
```

(`/dispatch-once` was the old spelling and the alias is gone. `/dispatch` is one bounded poll; `/start /dispatcher --max-polls N` is the version that takes a count.)

**If pre-recorded:** cut to the recorded segment and say plainly that this part is pre-recorded because it takes about four minutes of a model typing.

**On screen while it runs:** split between the ledger window and `git worktree list` in the target repo.

**Say:** the senior tier is a frontier coding agent running headless in a git worktree of its own, so it cannot touch your working tree. When it finishes, the system runs the verification commands the document declared. If they fail, or if the project declared none, no checkpoint is committed and the task is marked failed. A run that verified nothing does not get to call itself verified.

**Then show the checkpoint and the review:**

```bash
agent-ledger read_execution_ledger --workflow-name execute_work_unit | head -30
```

**Say:** implementation was Codex, review is Claude. Two different vendors, deliberately, so the reviewer is not the model that wrote the change and does not share its blind spots. The system staffs that from one config file, and when a provider's quota goes out it says the cross-check has collapsed rather than implying a second opinion it is not getting.

Check `configs/staffing.toml` before you record and say whichever way it is staffed that day; the claim the shot makes is that the two are *different*, not which one implements.

---

## 5:30 - 6:30 | It stops before the merge, and says where the work went

**On screen:** the pending `CODE_MERGE` approval.

**Say:** staff review approved, and the system still did not merge. That is on purpose: an automated fast-forward would remove the human reader without adding a gate.

Then approve it on camera, which is safe, because approving is not merging:

```bash
pi /approve-merge <approval_id>
```

**On screen:** the report, which now ends with `Queued for integration as <request_id>. No refinery run drains the queue yet, so the manual steps below are still how it lands.`

Then show that the queue is a durable row and not a printed sentence:

```bash
agent-ledger list_integration_requests
```

**Say:** approving is what puts the exact commit in the integration queue, bound to the approval that authorised it. The queue is a table, not a log line: it knows the branch, the base, the commit, and which approval it came from. What does not exist yet is the run that drains it, so the fast-forward is still a command for a person. The system tells you what it did not do, and it tells you where the work is waiting.

**Why this shot:** ending on a deliberate limitation is more persuasive than ending on a success, and it is the honest state of the project.
A printed string is indistinguishable from a system that forgot; a queued row with an id is the difference between "not implemented" and "implemented up to here", which is the claim actually being made.

**The refinery runs, and it does not change this shot.** `agent-ledger run_refinery <project>` builds the stack in a throwaway worktree and parks a merge conflict against the exact combination that refused it, and it never advances an integrated branch - so the sentence above stays true and the fast-forward is still a command for a person.
Do not show the runner unless you are willing to explain on camera why a thing that verified a stack then deliberately threw it away, which is a worse thirty seconds than the queue row.

**If you are re-shooting after milestone 4** of [refinery_integration_queue_design.md](refinery_integration_queue_design.md), this shot inverts: approve, and watch it build a stack, run the project's own verification commands on the combination, and fast-forward once.
Say plainly that the milestone still does not complete itself, because that is a further piece of work and the video should not imply otherwise.

---

## 6:30 - 7:30 | It survives things

Pick one. Both are real and both take under a minute.

**Option A, the reboot.** Kill the resident loops and show launchd bring them back:

```bash
./scripts/first-run-check.sh | tail -6   # shows who owns the loops
```

**Say:** the two processes that move queued work are supervised. After a reboot the machine comes back with the queue draining, not with every service healthy and nothing moving.

**Option B, the spent quota.** Show a usage-limit row in the ledger and the dispatcher restaffing around it.

**Say:** when a provider reports a usage limit, that is a row. The next dispatch reads it and staffs the tier somewhere else instead of rediscovering the wall by hitting it.

**Close on:** an agent session cannot resume itself. This can resume it.

---

## What to cut if the video runs long

In this order: the compile step (0:40), option B above, the cockpit detail view.
Keep the gate at 2:30 and the refusal to merge at 5:30. Those two are the argument.
Inside 5:30, the `list_integration_requests` shot is the first thing to cut and the approval report is the last: the sentence about what did not happen carries the argument, and the table is the evidence for it.

## What not to show

- The `.env`, and any terminal where it was printed.
- `configs/linked_projects.toml`, which names private repositories.
- Any GAWD draft under `docs/gawd_drafts/`.
- Frontier login flows, which put account identity on screen.
