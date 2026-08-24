# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The resident dispatcher, as a command something can supervise.

`LedgerDispatcher` has always been able to run forever. What it lacked was a way
to be started that was not "construct it inside a Pi directive", so the only way
to drain dispatch intents was a `/start /dispatcher` directive with a bounded
`max_polls`, and a resident dispatcher meant hand-writing a script.

This is the missing entrypoint, and it is deliberately the same shape as
`run_enqueue_drainer`. The two together are the pair a WorkUnit needs: the
drainer hands a WorkUnit to DBOS, the WorkUnit's milestones submit dispatch
intents, and this claims them.
"""

from __future__ import annotations

from typing import Any

from .resident_loop import ResidentLoop, ResidentLoopBusy, hold_resident_loop
from .store import err, ok


def run_ledger_dispatcher(
    interval_seconds: float = 2.0,
    max_polls: int | None = None,
    tier: str | None = None,
    name: str = "dispatcher",
) -> dict[str, Any]:
    """Claim and run PENDING dispatch intents until `max_polls`, or forever.

    `tier` left unset claims any tier, which is what a single resident process
    wants. Setting it is how you fan out one process per tier, and the database
    is the queue, so several may run at once without coordinating.

    Fanning out that way is a deliberate act with one `tier` per process. Two
    *unscoped* dispatchers are the accident this guards: they arise from
    starting the runtime in a second git worktree, they both claim any tier, and
    which checkout's code runs an intent becomes a coin flip. The database is
    what they contend for, so the database is where ownership is decided.
    """

    from ..dbos_app import launch_dbos
    from ..dispatcher import LedgerDispatcher
    from ..dispatcher_runner import build_dispatcher_runner
    from ..harness_availability import build_quota_claim_gate
    from ..runtime import build_runtime
    from ..staffing import dispatch_seat_counts, load_staffing

    with hold_resident_loop(ResidentLoop.LEDGER_DISPATCHER, scope=tier) as lease:
        if isinstance(lease, ResidentLoopBusy):
            return err(
                "resident_loop_busy",
                message=lease.describe(),
                loop=lease.loop.value,
                tier=tier,
                owner=lease.owner.to_payload() if lease.owner else None,
            )

        # A process whose whole job is settling intents that milestones are
        # parked on is the process that has to be able to wake them.
        #
        # `notify_dispatch_status_change` returns False when DBOS is not launched
        # here, and this loop never launched it. So a milestone's `DBOS.recv` was
        # never signalled by the process that completed its intent: it waited its
        # whole bound and then reported a timeout for work that had finished
        # minutes earlier. The enqueue drainer opens with the same call, for the
        # mirror-image reason - it is the process that hands work to DBOS.
        #
        # `launch_dbos` is a no-op where DBOS is unconfigured, so this costs
        # nothing on a machine that has none.
        launch_dbos()
        runtime = build_runtime()
        # No delegate is passed because `build_dispatcher_runner` builds the
        # resident one. This call used to be the whole bug: it named no delegate,
        # the runner was built with none, and every junior task this process
        # claimed was launched as `claude --model gemma4`. A resident dispatcher
        # is precisely the caller with no Pi directive to borrow a delegate from,
        # so the default has to be a working one rather than absence.
        # The lease above keeps this the one dispatcher process; concurrency
        # lives inside it. Sibling milestones submit their intents together and
        # every PENDING intent is runnable by construction, so the loop claims
        # up to each tier's free seats - the seat counts are staffing's
        # capacity numbers, read from the same file that staffs the pipelines.
        staffing = load_staffing(runtime.settings.config_dir / "staffing.toml")
        dispatcher = LedgerDispatcher(
            build_dispatcher_runner(runtime),
            name=name,
            tier=tier,
            settings=runtime.settings,
            seats=dispatch_seat_counts(staffing.bench),
            # A tier whose seating has reported a spent quota is not claimed
            # from at all, so its intents keep their place in the queue instead
            # of being spent against a provider that will refuse. The gate holds
            # the full staffing, so the frontier pair answers as a pair: both
            # seats claimable when a fallback pairing can take them, neither
            # when nothing declared can. This loop's own interval is what picks
            # them up again once the quota returns.
            tier_claimable=build_quota_claim_gate(staffing, settings=runtime.settings),
        )
        dispatched = dispatcher.dispatch_pending_intents(
            interval_seconds=interval_seconds,
            max_polls=max_polls,
        )
        return ok(
            dispatched=dispatched,
            polls=max_polls,
            tier=tier,
            outcomes=[
                {
                    "intent_id": outcome.intent_id,
                    "status": outcome.status,
                    "tier": outcome.tier,
                    "target_project_id": outcome.target_project_id,
                    "milestone_id": outcome.milestone_id,
                    "source": outcome.source,
                }
                for outcome in getattr(dispatcher, "last_outcomes", [])
            ],
        )


__all__ = ["run_ledger_dispatcher"]
