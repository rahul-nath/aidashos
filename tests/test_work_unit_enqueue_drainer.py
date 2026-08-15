# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The loop that walks the outbox bridge.

`start_work_unit` writes an enqueue row and nothing reads it, so a WorkUnit sat
there until a human ran a command. DBOS cannot bootstrap it: the outbox is a
coordination-database table DBOS has never heard of, and DBOS recovery only
resumes workflows it already started.
"""

from __future__ import annotations

from typing import Any

from work_unit_support import compile_acceptance_doc, install_simulated_engine

from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.enqueue_drainer import (
    Delivered,
    EnqueueDrainer,
    Idle,
    Stalled,
)
from local_first_agent_os.work_units.root_workflow import EnqueueDelivery


def _pending_work_unit(design_doc_id: str) -> str:
    """A WorkUnit with a pending enqueue row and nothing driving it yet."""

    compiled = compile_acceptance_doc(design_doc_id=design_doc_id)
    assert compiled.compiled_plan_revision_id is not None
    return repo.start_work_unit(compiled.compiled_plan_revision_id).work_unit.work_unit_id


def test_an_idle_outbox_reports_idle(work_unit_ledger: Any) -> None:
    assert EnqueueDrainer().poll_once() == Idle()


def test_an_empty_outbox_never_stalls_however_long_it_stays_empty(
    work_unit_ledger: Any,
) -> None:
    """Nothing pending is the steady state, not a fault.

    The stall counter exists for work that cannot move. An idle queue has no work,
    so a loop that escalated on it would escalate almost immediately and forever,
    which is the opposite of the signal being added.
    """

    drainer = EnqueueDrainer(stalled_after_passes=1)

    for _ in range(5):
        assert drainer.poll_once() == Idle()
    assert drainer.consecutive_undeliverable_passes == 0


def test_repeated_undeliverable_passes_escalate_to_stalled(work_unit_ledger: Any) -> None:
    """The signal that was missing when the drainer could never deliver.

    One undeliverable pass is ordinary - a runtime restarts, a pass lands in the
    gap. A run of them means the rows will never move, and before this the loop
    logged the same warning forever, which reads like weather rather than a fault.
    """

    work_unit_id = _pending_work_unit("drainer_stalled")
    drainer = EnqueueDrainer(stalled_after_passes=3)

    assert drainer.poll_once() == Idle(undeliverable=(work_unit_id,))
    assert drainer.poll_once() == Idle(undeliverable=(work_unit_id,))
    assert drainer.poll_once() == Stalled(
        undeliverable=(work_unit_id,),
        consecutive_passes=3,
    )


def test_one_delivery_clears_the_stall_counter(work_unit_ledger: Any) -> None:
    """Recovery is silent, because a loop that healed has nothing to report."""

    install_simulated_engine()
    _pending_work_unit("drainer_recovers")
    drainer = EnqueueDrainer(delivery=EnqueueDelivery.INLINE, stalled_after_passes=2)
    drainer.consecutive_undeliverable_passes = 1

    outcome = drainer.poll_once()

    assert isinstance(outcome, Delivered)
    assert drainer.consecutive_undeliverable_passes == 0


def test_a_pending_enqueue_is_undeliverable_without_a_durable_runtime(
    work_unit_ledger: Any,
) -> None:
    """The row stays pending, which is the whole point of the outbox.

    "No DBOS runtime is up" is a fact about the drainer process, not about the
    WorkUnit. Marking the row delivered here would lose an execution that never
    started; failing it would need an operator to notice and retry.
    """

    work_unit_id = _pending_work_unit("drainer_pending")

    outcome = EnqueueDrainer().poll_once()

    assert outcome == Idle(undeliverable=(work_unit_id,))
    assert [row.work_unit_id for row in repo.list_pending_enqueues(10)] == [work_unit_id]


def test_an_inline_drainer_delivers_and_clears_the_row(work_unit_ledger: Any) -> None:
    """INLINE is the escape hatch for a run with no DBOS, and it must still drain."""

    install_simulated_engine()
    work_unit_id = _pending_work_unit("drainer_inline")

    outcome = EnqueueDrainer(delivery=EnqueueDelivery.INLINE).poll_once()

    assert isinstance(outcome, Delivered)
    assert outcome.work_unit_ids == (work_unit_id,)
    assert repo.list_pending_enqueues(10) == ()


def test_the_loop_drains_a_backlog_before_it_sleeps(work_unit_ledger: Any) -> None:
    """A busy pass goes straight round again; only an idle one pays the interval.

    Sleeping between deliveries would make a backlog drain at one WorkUnit per
    interval, which is the difference between a queue and a metronome.
    """

    install_simulated_engine()
    _pending_work_unit("drainer_backlog_one")
    _pending_work_unit("drainer_backlog_two")
    slept: list[float] = []

    delivered = EnqueueDrainer(delivery=EnqueueDelivery.INLINE, limit=1).run(
        interval_seconds=30.0, max_polls=3, sleeper=slept.append
    )

    assert delivered == 2
    # Two busy passes, then one idle pass that sleeps once.
    assert slept == [30.0]


def test_the_loop_stops_after_max_polls(work_unit_ledger: Any) -> None:
    """Bounded runs are what make this testable and what a one-shot command uses."""

    slept: list[float] = []

    delivered = EnqueueDrainer().run(interval_seconds=1.0, max_polls=4, sleeper=slept.append)

    assert delivered == 0
    assert slept == [1.0, 1.0, 1.0, 1.0]


def test_a_phase_child_id_is_scoped_to_its_epoch(work_unit_ledger: Any) -> None:
    """The defect a real DBOS run would have hit first.

    The root continuation is `{root}:resume:{epoch}` and a milestone attempt is
    `{root}:milestone:{key}:{attempt}`, but the phase child was neither. Since
    `run_phase` returns normally when a milestone parks, the phase workflow
    completes and consumes its ID; an operator resume then re-entered the same
    one. DBOS does not re-run a workflow that already returned, so the resume
    either replayed the stale "still parked" result or was refused.

    Stable within an epoch is the other half: that is what lets a crash mid-phase
    resume the same execution rather than start a rival.
    """

    from local_first_agent_os.work_units.lifecycle import LifecyclePhase
    from local_first_agent_os.work_units.root_workflow import (
        ExecutionSnapshot,
        phase_workflow_id,
    )

    compiled = compile_acceptance_doc(design_doc_id="phase_epoch")
    assert compiled.compiled_plan_revision_id is not None
    unit = repo.start_work_unit(compiled.compiled_plan_revision_id).work_unit
    snapshot = ExecutionSnapshot(
        work_unit_id=unit.work_unit_id,
        root_workflow_id=unit.root_workflow_id,
        design_doc_revision_id=unit.design_doc_revision_id,
        compiled_plan_revision_id=unit.compiled_plan_revision_id,
        compiled_plan_hash=unit.compiled_plan_hash,
        lifecycle_profile_version=unit.lifecycle_profile_version,
        lifecycle_profile=unit.lifecycle_profile,
        title=unit.title,
        plan=repo.get_compiled_plan_revision(unit.compiled_plan_revision_id).plan,
    )
    phase = LifecyclePhase.PLAN

    first = phase_workflow_id(snapshot, phase, 0)
    resumed = phase_workflow_id(snapshot, phase, 1)

    assert first == phase_workflow_id(snapshot, phase, 0), "stable within an epoch"
    assert first != resumed, "a resume must not re-enter a workflow that already returned"
    assert first.startswith(unit.root_workflow_id)
    assert phase_workflow_id(snapshot, LifecyclePhase.VERIFY, 0) != first
