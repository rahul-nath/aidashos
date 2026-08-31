# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The start-time credential check, which is about this machine and not the work."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from local_first_agent_os.harness_readiness import (
    HarnessNotReady,
    HarnessReadiness,
    HarnessReady,
    HarnessUnknown,
    TierRestaffed,
    TierServed,
    TierStaffing,
    TierUnstaffable,
    effective_bench,
    frontier_harnesses_on_bench,
    plan_tier_staffing,
    probe_harness,
    readiness_refusals,
    restaffings,
    staffing_refusals,
)
from local_first_agent_os.staffing import (
    DEFAULT_BENCH,
    Bench,
    BenchSlot,
    FrontierHarness,
    Harness,
)
from local_first_agent_os.vocabulary import DispatchTier


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr="")


def _logged_out(harness: FrontierHarness) -> HarnessNotReady:
    return HarnessNotReady(harness=harness, detail="not signed in", remedy=f"{harness.value} login")


_UNSTAFFABLE_DETAIL = "claude: not signed in. Fix with: claude auth login"
"""A remedy string the refusal is expected to carry through verbatim.

Deliberately a literal rather than derived from the bench: what these scenarios
check is that the *provider's own* remedy reaches the operator unedited, so
computing it here would let a refusal that dropped it still pass.
"""


def _senior_vendor() -> FrontierHarness:
    """Whichever vendor the default bench seats as senior.

    These scenarios are about a tier moving to its ready peer, not about claude
    or codex. Naming the vendors made every one of them fail the day the bench
    was reseated, for a reason none of them is testing.
    """

    return FrontierHarness(DEFAULT_BENCH[DispatchTier.SENIOR].harness.value)


def _staff_vendor() -> FrontierHarness:
    return FrontierHarness(DEFAULT_BENCH[DispatchTier.STAFF].harness.value)


def _nothing_can_staff_senior(**_kwargs: object) -> tuple[TierStaffing, ...]:
    return (
        TierUnstaffable(
            tier=DispatchTier.SENIOR,
            configured=DEFAULT_BENCH[DispatchTier.SENIOR],
            detail=_UNSTAFFABLE_DETAIL,
        ),
    )


def _everything_staffed(**_kwargs: object) -> tuple[TierStaffing, ...]:
    return tuple(TierServed(tier=tier, configured=slot) for tier, slot in DEFAULT_BENCH.items())


def test_claude_is_read_by_its_field_and_not_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claude auth status` exits zero while logged out.

    Trusting the exit code would call a logged-out machine ready, which is the
    exact mistake this module exists to stop making at a more expensive layer.
    """

    monkeypatch.setattr(
        "local_first_agent_os.harness_readiness._run_probe",
        lambda _c: _completed('{"loggedIn": false, "authMethod": "none"}', returncode=0),
    )

    state = probe_harness(FrontierHarness.CLAUDE)

    assert isinstance(state, HarnessNotReady)
    assert "claude auth login" in state.remedy


def test_a_signed_in_claude_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.harness_readiness._run_probe",
        lambda _c: _completed('{"loggedIn": true, "authMethod": "oauth_token"}'),
    )

    assert isinstance(probe_harness(FrontierHarness.CLAUDE), HarnessReady)


def test_an_absent_cli_is_unknown_rather_than_logged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two different situations needing two different answers.

    Reporting a missing CLI as logged out sends an operator to re-run a login
    that was never the problem.
    """

    monkeypatch.setattr("local_first_agent_os.harness_readiness._run_probe", lambda _c: None)

    for harness in FrontierHarness:
        assert isinstance(probe_harness(harness), HarnessUnknown)


def test_codex_is_read_by_its_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.harness_readiness._run_probe",
        lambda _c: _completed(returncode=1),
    )

    state = probe_harness(FrontierHarness.CODEX)

    assert isinstance(state, HarnessNotReady)
    assert state.remedy == "codex login"


def test_an_all_local_bench_needs_no_frontier_account() -> None:
    """A supported configuration, and one this check must not refuse."""

    bench: Any = {tier: BenchSlot(harness=Harness.PI, model="gemma4") for tier in DispatchTier}

    assert frontier_harnesses_on_bench(bench) == frozenset()


def test_only_the_harnesses_the_bench_names_are_probed() -> None:
    bench: Any = {tier: BenchSlot(harness=Harness.PI, model="gemma4") for tier in DispatchTier}
    bench[DispatchTier.SENIOR] = BenchSlot(harness=Harness.CLAUDE)

    assert frontier_harnesses_on_bench(bench) == frozenset({FrontierHarness.CLAUDE})


def test_an_unanswerable_probe_is_not_treated_as_permission_to_start() -> None:
    """Starting anyway is how the expensive discovery got made the first time."""

    refusals = readiness_refusals(
        [
            HarnessReady(harness=FrontierHarness.CODEX),
            HarnessUnknown(harness=FrontierHarness.CLAUDE, detail="did not run"),
        ]
    )

    assert len(refusals) == 1
    assert "readiness unknown" in refusals[0]


def test_the_operator_door_refuses_before_it_writes_a_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check belongs at the command layer, not the service beneath it.

    `service.start_work_unit` is called by tests and by code with no business
    spawning a subprocess to read this machine. Putting the probe there made a
    core function environment-dependent and failed 71 tests; here it guards the
    one door a human actually opens.
    """

    from local_first_agent_os.work_units import commands

    started: list[str] = []
    monkeypatch.setattr(commands, "plan_tier_staffing", _nothing_can_staff_senior)
    monkeypatch.setattr(
        commands.service,
        "start_work_unit",
        lambda *a, **k: started.append("started") or {},
    )

    result = commands.start_work_unit("cpr_1")

    assert result["ok"] is False
    assert result["error"] == "harness_not_ready"
    assert "claude auth login" in result["message"]
    assert started == [], "no row may be written when the door refuses"


def test_a_ready_bench_is_not_stopped_at_the_door(monkeypatch: pytest.MonkeyPatch) -> None:
    from local_first_agent_os.work_units import commands

    monkeypatch.setattr(commands, "plan_tier_staffing", _everything_staffed)
    monkeypatch.setattr(commands.service, "start_work_unit", lambda *a, **k: {"work_unit_id": "w1"})

    result = commands.start_work_unit("cpr_1")

    assert result["ok"] is True
    assert result["work_unit_id"] == "w1"


def test_resuming_refuses_on_the_same_grounds_as_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The door that actually matters for a WorkUnit a dead CLI already blocked.

    Only `start_work_unit` carried this check at first, which guarded the path
    an operator takes once and left the path they take every time afterwards
    open. A blocked WorkUnit is reached by resuming, so resuming into a harness
    that has already said it cannot act reproduces the same failure and spends
    another attempt from the milestone's budget to learn nothing.
    """

    from local_first_agent_os.work_units import commands

    resumed: list[str] = []
    monkeypatch.setattr(commands, "plan_tier_staffing", _nothing_can_staff_senior)
    monkeypatch.setattr(
        commands.service,
        "resume_work_unit",
        lambda *a, **k: resumed.append("resumed") or {},
    )

    result = commands.resume_work_unit("wu_1")

    assert result["ok"] is False
    assert result["error"] == "harness_not_ready"
    assert "claude auth login" in result["message"]
    assert resumed == [], "no attempt may be spent when the door refuses"


def test_a_ready_bench_may_still_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    from local_first_agent_os.work_units import commands

    monkeypatch.setattr(commands, "plan_tier_staffing", _everything_staffed)
    monkeypatch.setattr(
        commands.service, "resume_work_unit", lambda *a, **k: {"work_unit_id": "w1"}
    )

    result = commands.resume_work_unit("w1")

    assert result["ok"] is True
    assert result["work_unit_id"] == "w1"


def test_the_reconciler_path_is_deliberately_not_gated() -> None:
    """A resident loop must not shell out to probe this machine on every pass.

    `CrashReconciler` calls `service.resume_work_unit`, below the door, and is
    bounded by `max_automatic_recoveries` instead. Asserted rather than left to
    a comment because the obvious "fix" for the gap this file pins is to push
    the probe down into the service, which is exactly the change that made a
    core function environment-dependent and failed 71 tests.
    """

    import inspect

    from local_first_agent_os.work_units import crash_recovery_loop, service

    source = inspect.getsource(crash_recovery_loop)
    assert "service.resume_work_unit" in source
    assert "commands.resume_work_unit" not in source
    assert "check_frontier_readiness" not in inspect.getsource(service)


# --- Re-staffing: a down provider moves the tier instead of stopping the run ---


def test_a_logged_out_provider_moves_the_tier_to_its_ready_peer() -> None:
    """The whole point. Refusing here stopped runs a ready peer could have taken.

    A logged-out senior vendor says nothing about whether the staff vendor can
    implement, and the old check refused the entire run on the harness answer
    alone.
    """

    plan = plan_tier_staffing(
        states=(
            _logged_out(_senior_vendor()),
            HarnessReady(harness=_staff_vendor()),
        )
    )

    senior = next(item for item in plan if item.tier is DispatchTier.SENIOR)
    assert isinstance(senior, TierRestaffed)
    assert senior.replacement.harness is DEFAULT_BENCH[DispatchTier.STAFF].harness
    assert staffing_refusals(plan) == (), "a covered tier is not grounds to refuse"


def test_the_run_is_refused_only_when_no_peer_can_cover_the_tier() -> None:
    plan = plan_tier_staffing(
        states=(
            _logged_out(FrontierHarness.CLAUDE),
            _logged_out(FrontierHarness.CODEX),
        )
    )

    refusals = staffing_refusals(plan)

    assert refusals, "nothing on the bench can act, which is the one blocking case"
    assert any("senior" in refusal for refusal in refusals)


def test_a_tier_that_moved_is_never_silent() -> None:
    """The bench is an operator decision and this is the system not following it."""

    plan = plan_tier_staffing(
        states=(
            _logged_out(_senior_vendor()),
            HarnessReady(harness=_staff_vendor()),
        )
    )

    notices = restaffings(plan)

    assert len(notices) == 1
    assert f"senior moves from {_senior_vendor().value} to {_staff_vendor().value}" in notices[0]


def test_an_unanswerable_probe_does_not_move_a_tier() -> None:
    """Not knowing is reported, never acted on - the same rule the probe layer has.

    Moving a tier on `HarnessUnknown` would trade a refusal we understand for a
    run we do not.
    """

    plan = plan_tier_staffing(
        states=(
            HarnessUnknown(harness=_senior_vendor(), detail="did not run"),
            HarnessReady(harness=_staff_vendor()),
        )
    )

    senior = next(item for item in plan if item.tier is DispatchTier.SENIOR)
    assert isinstance(senior, TierServed)
    assert staffing_refusals(plan) == ()


def test_a_frontier_tier_is_never_handed_to_the_local_model() -> None:
    """The distinction between recovering and silently substituting.

    Junior is staffed to the local harness and is always available, so a
    fallback that took "any harness that can answer" would quietly hand senior
    implementation work to gemma and report it as done.
    """

    plan = plan_tier_staffing(
        states=(
            _logged_out(FrontierHarness.CLAUDE),
            _logged_out(FrontierHarness.CODEX),
        )
    )

    senior = next(item for item in plan if item.tier is DispatchTier.SENIOR)
    assert isinstance(senior, TierUnstaffable)


def test_a_moved_tier_keeps_its_own_capacity_and_takes_the_peers_knobs() -> None:
    """Capacity is the tier's; harness, model, and effort belong to the provider.

    `reasoning_effort` is a per-provider dial, so carrying it across would
    configure the replacement with the failed provider's setting.
    """

    plan = plan_tier_staffing(
        states=(
            _logged_out(_senior_vendor()),
            HarnessReady(harness=_staff_vendor()),
        )
    )
    bench = effective_bench(plan)

    assert bench[DispatchTier.SENIOR].harness is DEFAULT_BENCH[DispatchTier.STAFF].harness
    assert bench[DispatchTier.SENIOR].capacity == DEFAULT_BENCH[DispatchTier.SENIOR].capacity
    assert (
        bench[DispatchTier.SENIOR].reasoning_effort
        == DEFAULT_BENCH[DispatchTier.STAFF].reasoning_effort
    )
    assert bench[DispatchTier.STAFF].harness is DEFAULT_BENCH[DispatchTier.STAFF].harness, (
        "an untouched tier is unchanged"
    )


def test_an_all_local_bench_plans_without_asking_any_provider() -> None:
    local_bench: Bench = {tier: BenchSlot(harness=Harness.PI) for tier in DispatchTier}

    plan = plan_tier_staffing(bench=local_bench, states=())

    assert all(isinstance(item, TierServed) for item in plan)
    assert staffing_refusals(plan) == ()


def test_the_door_starts_the_run_when_a_tier_merely_moved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stated at the door, because that is where the refusal used to happen."""

    from local_first_agent_os.work_units import commands

    def _moved(**_kwargs: object) -> tuple[TierStaffing, ...]:
        return (
            TierRestaffed(
                tier=DispatchTier.SENIOR,
                configured=DEFAULT_BENCH[DispatchTier.SENIOR],
                replacement=DEFAULT_BENCH[DispatchTier.STAFF],
                detail="claude: not signed in",
            ),
        )

    monkeypatch.setattr(commands, "plan_tier_staffing", _moved)
    monkeypatch.setattr(commands.service, "start_work_unit", lambda *a, **k: {"work_unit_id": "w1"})

    result = commands.start_work_unit("cpr_1")

    assert result["ok"] is True, "a covered tier must not stop the run"
    assert result["work_unit_id"] == "w1"


# --- The door and the dispatcher tell one story about one outage ---------------


def _matrix_staffing():
    """Two vendors, each pairing declaring the other as its way out."""

    from local_first_agent_os.staffing import FrontierPairing, Staffing

    claude_only = FrontierPairing(
        name="claude-only",
        senior=BenchSlot(harness=Harness.CLAUDE, model="claude-opus-5", capacity=2),
        staff=BenchSlot(harness=Harness.CLAUDE, model="claude-fable-5", capacity=1),
        fallback=("codex-only",),
    )
    codex_only = FrontierPairing(
        name="codex-only",
        senior=BenchSlot(harness=Harness.CODEX, model="gpt-5.6-terra", capacity=2),
        staff=BenchSlot(harness=Harness.CODEX, model="gpt-5.6-sol", capacity=1),
        fallback=("claude-only",),
    )
    return Staffing(
        pairings={p.name: p for p in (claude_only, codex_only)},
        seated=claude_only,
        solo={DispatchTier.JUNIOR: BenchSlot(harness=Harness.PI, model="gemma4", capacity=4)},
    )


def test_a_logged_out_vendor_moves_the_whole_pair_at_the_door() -> None:
    """The door restaffs by pairing, exactly as the dispatch path does.

    It used to restaff per tier onto a ready peer's slot, so one outage got two
    accounts: the door told a human that senior had moved onto the staff seat's
    model, while the dispatcher moved the pair to a pairing an operator had
    actually declared and checked. The door's version is the one a human reads
    before granting execution, so it was the more expensive of the two to have
    wrong.
    """

    plan = plan_tier_staffing(
        bench=_matrix_staffing(),
        states=(
            HarnessNotReady(harness=FrontierHarness.CLAUDE, detail="logged out", remedy="log in"),
            HarnessReady(harness=FrontierHarness.CODEX),
        ),
    )
    moved = {item.tier: item for item in plan if item.tier is not DispatchTier.JUNIOR}

    senior, staff = moved[DispatchTier.SENIOR], moved[DispatchTier.STAFF]
    assert isinstance(senior, TierRestaffed) and isinstance(staff, TierRestaffed)
    assert (senior.slot.harness, senior.slot.model) == (Harness.CODEX, "gpt-5.6-terra")
    assert (staff.slot.harness, staff.slot.model) == (Harness.CODEX, "gpt-5.6-sol")
    assert "the pair moves to pairing 'codex-only'" in senior.detail
    assert staffing_refusals(plan) == ()


def test_an_unprobed_fallback_is_not_moved_onto() -> None:
    """`HarnessUnknown` on the escape is not evidence the escape works.

    Trading a refusal we understand for a run we do not is what
    `_ready_frontier_peer` always refused, and the pairing path inherits the
    rule rather than restating it loosely: a candidate pairing needs every
    vendor it depends on to have answered ready.
    """

    plan = plan_tier_staffing(
        bench=_matrix_staffing(),
        states=(
            HarnessNotReady(harness=FrontierHarness.CLAUDE, detail="logged out", remedy="log in"),
            HarnessUnknown(harness=FrontierHarness.CODEX, detail="probe did not run"),
        ),
    )
    frontier = [item for item in plan if item.tier is not DispatchTier.JUNIOR]

    assert all(isinstance(item, TierUnstaffable) for item in frontier)
    assert len(staffing_refusals(plan)) == 2


def test_an_unprobed_seated_vendor_is_reported_rather_than_acted_on() -> None:
    """Not knowing is not a reason to move a seat off the operator's choice."""

    plan = plan_tier_staffing(
        bench=_matrix_staffing(),
        states=(
            HarnessUnknown(harness=FrontierHarness.CLAUDE, detail="probe did not run"),
            HarnessReady(harness=FrontierHarness.CODEX),
        ),
    )
    frontier = [item for item in plan if item.tier is not DispatchTier.JUNIOR]

    assert all(isinstance(item, TierServed) for item in frontier)


def test_the_door_probes_the_escapes_too() -> None:
    """A door that probed only the seated vendors could never use a fallback.

    Every escape would come back `HarnessUnknown`, the planner would decline to
    move onto it - correctly - and the door would refuse an outage a declared,
    working fallback could have absorbed.
    """

    staffing = _matrix_staffing()

    assert frontier_harnesses_on_bench(staffing) == frozenset(
        {FrontierHarness.CLAUDE, FrontierHarness.CODEX}
    )
    # The seated pairing alone names only claude; the codex half is reachable
    # solely through the fallback declaration.
    assert staffing.seated.frontier_harnesses() == frozenset({FrontierHarness.CLAUDE})


def test_the_operator_door_passes_one_whole_staffing_to_probe_and_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command must not erase the pairing before either decision sees it."""

    from local_first_agent_os.work_units import commands

    staffing = _matrix_staffing()
    states = (HarnessReady(harness=FrontierHarness.CODEX),)
    seen: dict[str, object] = {}

    monkeypatch.setattr(commands, "load_staffing", lambda _path: staffing)

    def _availability(received: object) -> tuple[HarnessReadiness, ...]:
        seen["availability"] = received
        return states

    def _plan(*, bench: object, states: object) -> tuple[TierStaffing, ...]:
        seen["plan_bench"] = bench
        seen["plan_states"] = states
        return ()

    monkeypatch.setattr(commands, "check_harness_availability", _availability)
    monkeypatch.setattr(commands, "plan_tier_staffing", _plan)

    assert commands._harness_refusal() is None
    assert seen == {
        "availability": staffing,
        "plan_bench": staffing,
        "plan_states": states,
    }


def test_a_restaffed_seat_keeps_the_workload_profiles_it_arrived_with() -> None:
    """The cheap reading profile must survive the move, or the outage costs more.

    Observed on work unit c88ff4167c66 (2026-08-30): `TierRestaffed.slot`
    rebuilt the seat field by field and left `workload_profiles` behind, so
    `resolve_bench_for_workload` found no profile and handed both
    independent-reading tasks the full seat model. The file declared
    `claude-sonnet-5` for that phase; `claude-opus-5` and `claude-fable-5` ran
    it, and the Fable reading exhausted that model's credits. A restaffing is
    exactly when the cheap profile matters most, so dropping it there is the
    worst possible place to drop it.
    """

    from dataclasses import replace as _replace

    from local_first_agent_os.staffing import (
        JudgmentWorkload,
        WorkloadModelProfile,
        resolve_bench_for_workload,
    )

    reading = WorkloadModelProfile(
        workload=JudgmentWorkload.INDEPENDENT_READING,
        model="claude-sonnet-5",
        reasoning_effort="medium",
    )
    replacement = _replace(
        DEFAULT_BENCH[DispatchTier.STAFF],
        harness=Harness.CLAUDE,
        model="claude-fable-5",
        workload_profiles=(reading,),
    )
    moved = TierRestaffed(
        tier=DispatchTier.STAFF,
        configured=DEFAULT_BENCH[DispatchTier.STAFF],
        replacement=replacement,
        detail="codex: usage limit",
    )

    slot = moved.slot
    assert slot.workload_profiles == (reading,)

    resolved = resolve_bench_for_workload(
        DispatchTier.STAFF,
        JudgmentWorkload.INDEPENDENT_READING,
        {DispatchTier.STAFF: slot},
    )
    assert resolved.model == "claude-sonnet-5"
    assert resolved.reasoning_effort == "medium"
    # The standard workload still gets the seat's own model, so the profile is
    # a phase-scoped swap rather than a demotion of the whole seat.
    standard = resolve_bench_for_workload(
        DispatchTier.STAFF,
        JudgmentWorkload.STANDARD,
        {DispatchTier.STAFF: slot},
    )
    assert standard.model == "claude-fable-5"
