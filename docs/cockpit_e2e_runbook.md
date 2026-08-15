# Running a WorkUnit from the cockpit, end to end

Every command below was run on 2026-07-31 against the live Postgres, from this worktree, except step 6 which is marked and was deliberately not run.
Where a step is a terminal command rather than a button, that is because there is no route for it yet, and the reason is named.

## What the cockpit can and cannot do

Verified by reading the running server's own schema (`GET /openapi.json`), not by reading the frontend.

The application serves seven WorkUnit routes: list, detail, events, artifacts, decisions, cancel, resume.
`POST /authoring/design-docs/compile` compiles a DesignDoc and `POST /work-units` creates a WorkUnit, both landed in `f3a3dbf`, so the terminal commands recorded below are one way to run those steps rather than the only way.
The enqueue outbox has no route and needs none: the drainer is a resident loop - started by `./scripts/start-agent-runtime.sh`, or kept alive across reboots by `./scripts/launchd/install.sh` - so there is nothing for a button to trigger.
The cockpit also has one runtime button, `Run dispatcher`, which posts `/start /dispatcher --max-polls 5` to `POST /pi/directive`.

The honest gap is that a *deliberately paused* demo, where you want to watch each step happen, still means not starting the runtime and driving the steps by hand, exactly as below.

## 0. Bring up the ledger and the console

The ledger commands below are typed as `agent-ledger`, which is the `[project.scripts]` entry for `local_first_agent_os.coordination.cli:main`.
`./scripts/install_pi_shell.sh` puts it on `PATH` along with `pi`; without that, `uv run agent-ledger ...` from the repo root is the same command.
`uv run python agent_coordination_mcp.py ...` also still works and always will, because that file imports the same `main`.

Postgres first. It is the coordination ledger and the DBOS system database both.

```bash
uv run python scripts/start_postgres_docker.py
```

Then the API and the web client, in their own terminal. This leaves them in the foreground so closing the terminal stops them.

```bash
./scripts/start-ui-stack.sh
```

That serves FastAPI on `http://127.0.0.1:8000` and the console on `http://127.0.0.1:5173`.
The first run installs the web dependencies, which takes a minute; `web/node_modules` is not checked in.

Do **not** run `./scripts/start-agent-runtime.sh` yet if you want to watch each step happen.
It starts two resident loops, the enqueue drainer and the ledger dispatcher, and the dispatcher will claim any pending intent and spawn a real frontier agent without asking.
Start it when you want the lane to run unattended, and use the steps below when you want to drive it.

**Not starting it is not the same as it not running.** If `./scripts/launchd/install.sh` was ever run on this machine, launchd supervises those two loops, so `./scripts/stop-agent-runtime.sh` kills them and launchd restarts them within seconds. Check who actually owns them:

```bash
agent-ledger describe_resident_loops
```

An `owned` loop with a pid whose parent is launchd will outlive any stop script. To pause one for a driven run:

```bash
launchctl bootout gui/$(id -u)/com.rahul.local-first-agent.ledger-dispatcher
```

`./scripts/launchd/install.sh` puts it back. Leaving it supervised is the right steady state and the wrong setup for watching each step happen.

## 1. One terminal for the ledger environment

Never `source .env`. Export what you need.

```bash
cd "$(git rev-parse --show-toplevel)"
```

```bash
export LOCAL_AGENT_COORDINATION_BACKEND=postgres
export LOCAL_AGENT_COORDINATION_DATABASE_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:5432/local_agent"
export AGENT_COORDINATION_BACKEND=postgres
export AGENT_COORDINATION_DATABASE_URL="$LOCAL_AGENT_COORDINATION_DATABASE_URL"
export LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:5432/local_agent_dbos"
export DBOS_SYSTEM_DATABASE_URL="$LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL"
export LOCAL_AGENT_USE_DBOS=true
```

`LOCAL_AGENT_DATABASE_URL` is deliberately absent.
It names the DBOS system database, and feeding it to the coordination store produces a psycopg error a long way from the mistake.

## 2. Compile the design doc

```bash
agent-ledger compile_design_doc docs/runtime_lifetime_follows_work_gawd.md
```

This runbook's original example was `docs/completed/agent_acl_enforcement_gawd.md`.
That WorkUnit reached `SUCCEEDED`/`COMPLETE` on 2026-08-11 and must not be re-run: its code is on main, so a re-run dispatches agents at finished work and fails the `source_patch` evidence gate on empty patches.
Substitute whichever design doc you are actually running; the steps do not change.

Read three fields of the answer before going on.
`runnable` must be `true`, `execution_blockers` must be empty, and `compiled_plan_revision_id` is what the next step takes.

A compiled plan revision is content addressed by its plan hash, so compiling the same document twice returns the same id rather than a second plan.
Editing the document produces a new one, and the old revision stays as history.

Earlier revisions of this document are already in your ledger and are not in your way.
Content addressing is what makes that true: compiling unchanged bytes returns the existing id instead of writing a duplicate, and compiling changed bytes writes a new revision beside the old one.
So this step is the real beginning of the run whether or not the document has been compiled before, and nothing needs clearing first.

## 3. Start the WorkUnit

```bash
agent-ledger start_work_unit <compiled_plan_revision_id> --title "runtime lifetime follows work"
```

This writes the WorkUnit, its milestones, and the enqueue outbox row in one transaction.
The answer includes `"status": "QUEUED"` and a `dispatch` block.

If the drainer is not running, that block reads `"delivered": false, "reason": "no active DBOS runtime; the enqueue stays pending"`.
That is correct rather than a failure: the outbox row is durable and the next drainer poll picks it up.

## 4. Watch it in the cockpit

Open `http://127.0.0.1:5173` and scroll to **GOVERNED WORK**.
Pick the WorkUnit from the `WORKUNIT` dropdown, which lists it by the title you gave `start_work_unit`, e.g. `runtime lifetime follows work · QUEUED`.

**`QUEUED` is what you see only if no DBOS runtime is up, and step 0 started one.**
`./scripts/start-ui-stack.sh` runs the API, whose lifespan launches DBOS, so a WorkUnit started after it is delivered immediately and is already `RUNNING` by the time you look. That is correct behaviour, not a fault: the outbox exists for the case where nothing can take the work, and something can.

Following this document straight through, expect `RUNNING` here, `PLAN` already past `CLARIFY` and `VALIDATE`, and the run parked at a `DISPATCH_INTENT_CREATED` waiting for step 6. Step 5 then reports `Idle`, which in that situation means the runtime already took it rather than what step 5 says it means.

To see `QUEUED`, stop the API before step 3 and start it again afterwards.

What you should see with no runtime up, confirmed against a running console:

- A status chip reading `QUEUED` and a phase chip reading `CLARIFY`.
- The seven-phase strip, CLARIFY through DELIVER, all `PENDING`.
- **Identity**, carrying the WorkUnit id, the root workflow id, the DesignDoc revision, the compiled plan revision, the plan hash, and the lifecycle version.
- **Milestones**, all eight, each with its phase, its dependencies, and an evidence cell reading `missing implementation_plan`, `missing source_patch`, and so on. That column is the evidence gate saying in advance what each milestone owes.
- **Evidence**, reading `No evidence has been recorded yet.`
- **History**, with `WORK_UNIT_CREATED`, `PLAN_BOUND`, and `ROOT_WORKFLOW_ENQUEUED`.

The two lanes poll on their own, the summary every five seconds and the events every three, so leave the tab open rather than refreshing it.

## 5. Hand it to DBOS

The drainer is the loop that moves a QUEUED WorkUnit into a running DBOS workflow.
There is no cockpit button for it, so run one bounded pass:

```bash
agent-ledger run_enqueue_drainer --max-polls 1 --interval-seconds 0
```

Read the outcome rather than the exit code.

`Idle` means the outbox held nothing to deliver, and there are two ways to get there. The ordinary one, following this document straight through, is that a DBOS runtime was already up from step 0 and took the WorkUnit at step 3; the cockpit shows it `RUNNING` and this pass had nothing left to do. The other is that this shell points at a different ledger than the one you started the WorkUnit in, which `agent-ledger list_work_units` settles in one command: if it lists your WorkUnit, the first reading is the right one.

`Stalled` means rows are there and cannot move, which almost always means `LOCAL_AGENT_USE_DBOS` is not `true` in this shell.
A delivery reports the work unit id.

The cockpit should now show the WorkUnit leave QUEUED, skip CLARIFY and VALIDATE, enter PLAN, and stop at `DISPATCH_INTENT_CREATED`.
Parking there is correct: the milestone has asked for an agent and nothing has claimed it yet.

For a resident drainer instead of one pass, drop `--max-polls` and leave it running.

## 6. Let an agent claim it

**Stop and read this before pressing the button.**

The dispatcher spawns a real frontier CLI against the intent.
It costs money, it takes time, and it will create a git worktree and edit files in it.

That milestone has since landed, so what follows is no longer the task-name boolean this section used to warn about.
`src/local_first_agent_os/spawn_authority.py` derives the spawn posture from the task's capabilities, intersected with the ceiling its dispatch intent carries.
A bypass is emitted if and only if the capability set holds both `write_repository` and `run_command`; a task holding only `run_command` gets a shell with no editor, and anything less gets a read-only spawn with the mutating tools named as forbidden.
So a planning or review task no longer receives `--dangerously-skip-permissions`, and an implementation task still does, in a worktree, inheriting this shell's environment.

When you want that, the cockpit can do it: the **Runtime** panel inside GOVERNED WORK has a `Run dispatcher` button which claims up to five pending intents.
The equivalent terminal command is:

```bash
agent-ledger run_ledger_dispatcher --max-polls 1
```

I ran the cockpit's button path with `--max-polls 1` against an empty queue to prove the plumbing, and it returned in 2.1 seconds with `dispatched 0 intent(s) in 1 poll(s)`.
That proves the route, the directive, and the coordination calls behind it work.
It proves nothing about dispatch, because there was nothing to dispatch, and a check that passes on empty input is the failure mode this codebase keeps rediscovering.

## 7. Approve, cancel, resume

At the REVIEW milestone the WorkUnit parks on an operator decision and the cockpit shows it under **Waiting on you** with APPROVE and DENY.
Those post to `POST /work-units/{id}/decisions` and are the one write the cockpit has always had.

`POST /work-units/{id}/cancel` and `POST /work-units/{id}/resume` exist on the server and no button calls them yet.
Until one does:

```bash
agent-ledger cancel_work_unit <work_unit_id> --reason "..."
```

Know what cancel does before relying on it.
It moves the WorkUnit to `CANCELLING`, then stops what can be stopped concurrently: a still-pending dispatch intent, the DBOS workflows, and the execution leases whose supervisors own the agent process groups.
Only then does it write `CANCELLED`.
Read `refused` in the result.
A lease cancel is cooperative, so an agent stops at its supervisor's next heartbeat rather than instantly, and anything reported as refused is something no part of this stopped, which is your cue to kill a process by hand.

## What to look at when it goes wrong

Events, in order, from the ledger rather than the screen:

```bash
agent-ledger list_work_unit_events <work_unit_id>
```

Whether DBOS ever ran anything, and how it ended:

```bash
agent-ledger read_execution_ledger
```

The evidence a milestone produced, which is the moment of truth:

```bash
agent-ledger list_work_unit_artifacts <work_unit_id>
```

Read the artifact bodies, not just their types.
Evidence is derived from the agent's own run report now: a `source_patch` carries the files it changed, a `test_result` the commands it ran and their output, and the advisory kinds its written summary.
Two artifacts of different types with byte-identical bodies would mean the old fan-out defect had returned; they should differ, because they are derived from different parts of the report.

A milestone that fails with `missing_required_artifacts` is the gate working, not a bug in the harness.
It means the agent finished without producing evidence of that kind, and the commonest case is an `IMPLEMENT` milestone whose agent changed no files.
Read its dispatch result before assuming the harness is at fault.

A milestone that fails with `unverifiable_dispatch_result` means the intent was completed by something other than the runner, usually by hand, so there is no `dispatch_runner_result.v1` payload to read evidence from.

## What has been run, and what has not

The ledger is the authority here; query it rather than trusting prose:

```bash
agent-ledger list_work_units
```

As of 2026-08-09 the operator ledger holds four WorkUnits.
The furthest, `agent ACL enforcement` (`82bdb0c6`), ran steps 3 through 6 for real: DBOS executed its root workflow, the PLAN milestone created a dispatch intent, a dispatcher settled it, and the milestone reached `MILESTONE_SUCCEEDED`.
Its two IMPLEMENT milestones blocked on 2026-08-06 with `dispatch_failure_evidence` artifacts, so the WorkUnit sits `BLOCKED` at IMPLEMENT; read the artifacts before assuming a cause.

What has not yet happened on the operator ledger:

- an IMPLEMENT milestone completing, with `source_patch` evidence from a real senior run;
- a REVIEW milestone resolved through the cockpit's APPROVE button, which is step 7 against a real parked WorkUnit;
- a WorkUnit reaching `SUCCEEDED` outside the test suite.
  `tests/test_work_unit_golden_path.py` drives exactly that trip through real subprocess resident loops with every tier on the local model, so the seams are exercised in the suite; the operator ledger is what has not recorded it end to end.

Either of two actions retires that list, and both fit a pre-demo checklist: resume `82bdb0c6` after reading `agent-ledger list_work_unit_artifacts 82bdb0c60ae3e07b96bed01918191a1d`, or run this runbook once, start to `SUCCEEDED`.
