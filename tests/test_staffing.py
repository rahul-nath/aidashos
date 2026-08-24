# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from local_first_agent_os.harness_availability import staffing_around_spent_quotas
from local_first_agent_os.harness_readiness import TierRestaffed, TierServed, TierUnstaffable
from local_first_agent_os.pow_wow import CliPowWowExecutor
from local_first_agent_os.pow_wow.protocol import PlanningPhase, TaskPurpose
from local_first_agent_os.pow_wow.types import PowWowTaskSpec
from local_first_agent_os.spawn_authority import ReadOnlyInspection, UnattendedImplementation
from local_first_agent_os.staffing import (
    DEFAULT_BENCH,
    DEFAULT_ROSTERS,
    BackupModel,
    BenchSlot,
    CheckRole,
    FrontierHarness,
    FrontierPairing,
    Harness,
    JudgmentRole,
    JudgmentWorkload,
    Roster,
    SharedSeatRefused,
    Tier,
    WorkloadModelProfile,
    dispatch_seat_counts,
    load_bench,
    load_staffing,
    resolve_bench,
    resolve_bench_for_workload,
)


def test_default_bench_seats_two_different_frontier_vendors() -> None:
    """The fallback bench keeps the one property the seating exists for.

    `DEFAULT_BENCH` is reached only when no staffing.toml exists. Which vendor
    holds which seat there is currently allowed to differ from the repo config,
    because a fleet of tests scripts its scenarios against the default seating;
    what may never differ is the invariant that the two frontier seats are two
    vendors, so that is what this asserts.
    """

    frontier_seats = {resolve_bench(Tier.SENIOR).harness, resolve_bench(Tier.STAFF).harness}
    assert frontier_seats == {Harness.CLAUDE, Harness.CODEX}
    junior = resolve_bench(Tier.JUNIOR)
    assert junior.harness == Harness.PI
    assert junior.model == "gemma4"
    # qwen kept as a backup junior model for the upcoming local comparison eval
    assert junior.backup_models == (BackupModel(harness=Harness.PI, model="qwen3.8-27b-mtp"),)


def test_capacity_encodes_allocation() -> None:
    assert resolve_bench(Tier.STAFF).capacity == 1
    assert resolve_bench(Tier.SENIOR).capacity == 3
    assert resolve_bench(Tier.JUNIOR).capacity == 4


def test_dispatch_seat_counts_are_the_bench_capacities() -> None:
    """The dispatcher's per-tier seats are staffing's capacity numbers, verbatim.

    Keyed by tier value because the dispatcher speaks the ledger's tier strings.
    A tier absent from the bench gets no key: an unstaffed tier has no seats.
    """

    bench = {
        Tier.SENIOR: BenchSlot(harness=Harness.CODEX, capacity=3),
        Tier.STAFF: BenchSlot(harness=Harness.CLAUDE, capacity=1),
    }
    assert dispatch_seat_counts(bench) == {"senior": 3, "staff": 1}
    assert dispatch_seat_counts() == {
        tier.value: slot.capacity for tier, slot in DEFAULT_BENCH.items()
    }


def test_stage_role_is_a_sum_type() -> None:
    # A judgment role carries a tier and (optionally) a stance; a check carries a command.
    judge = JudgmentRole(name="reviewer", tier=Tier.STAFF, stance="evaluator")
    check = CheckRole(name="test_runner", command="uv run pytest")
    assert judge.kind == "judgment" and judge.tier == Tier.STAFF and judge.stance == "evaluator"
    assert check.kind == "check" and check.command == "uv run pytest"
    # They are distinct types; a check has no tier, a judge has no command.
    assert not hasattr(check, "tier")
    assert not hasattr(judge, "command")


def test_default_rosters_encode_two_models_checking_each_other() -> None:
    impl = DEFAULT_ROSTERS["IMPLEMENTATION"]
    review = DEFAULT_ROSTERS["REVIEW"]
    assert impl.judgment[0].name == "implementer"
    assert impl.judgment[0].tier == Tier.SENIOR
    assert review.judgment[0].name == "reviewer"
    assert review.judgment[0].tier == Tier.STAFF  # the other vendor checks the implementer's work
    assert review.judgment[0].stance == "evaluator"
    # consensus panel names come straight from the staffing model, not hardcoded strings
    assert {role.name for role in review.consensus} == {"reviewer", "qa", "realist"}


def test_load_bench_falls_back_to_defaults_when_absent(tmp_path: Path) -> None:
    assert load_bench(tmp_path / "nope.toml") == DEFAULT_BENCH


def test_load_bench_reads_toml(tmp_path: Path) -> None:
    cfg = tmp_path / "staffing.toml"
    cfg.write_text(
        """
seated_pairing = "pair"

[pairings.pair.senior]
harness = "codex"
capacity = 3

[pairings.pair.staff]
harness = "claude"
capacity = 2

[pairings.pair.staff.workloads.independent_reading]
model = "claude-sonnet-5"
reasoning_effort = "medium"

[bench.junior]
harness = "pi"
model = "gemma4"
capacity = 8
""".strip(),
        encoding="utf-8",
    )
    bench = load_bench(cfg)
    assert bench[Tier.STAFF] == BenchSlot(
        harness=Harness.CLAUDE,
        model=None,
        capacity=2,
        workload_profiles=(
            WorkloadModelProfile(
                workload=JudgmentWorkload.INDEPENDENT_READING,
                model="claude-sonnet-5",
                reasoning_effort="medium",
            ),
        ),
    )
    assert bench[Tier.SENIOR] == BenchSlot(harness=Harness.CODEX, model=None, capacity=3)
    assert bench[Tier.JUNIOR] == BenchSlot(harness=Harness.PI, model="gemma4", capacity=8)


def test_a_frontier_seat_cannot_be_declared_alone(tmp_path: Path) -> None:
    """Half a pair is refused at load, which is the whole point of pairing them.

    Two independent tables let an operator change the implementer and leave the
    reviewer pointing wherever it already pointed. That is not hypothetical: the
    2026-08-09 swap left four prose claims behind, and POLICIES.md twice denied
    the implementer a write its own plan had granted, because two declarations
    disagreed about who was seated.
    """

    cfg = tmp_path / "staffing.toml"
    cfg.write_text(
        """
[bench.senior]
harness = "claude"
model = "claude-opus-5"

[bench.junior]
harness = "pi"
model = "gemma4"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"\[bench.senior\] seats half of a pair"):
        load_bench(cfg)


def test_a_pairing_needs_both_seats(tmp_path: Path) -> None:
    """A table naming one seat is not a pairing with a blank in it."""

    cfg = tmp_path / "staffing.toml"
    cfg.write_text(
        """
seated_pairing = "half"

[pairings.half.senior]
harness = "claude"
model = "claude-opus-5"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="declares no staff seat"):
        load_bench(cfg)


def test_the_seated_pairing_is_named_rather_than_assumed(tmp_path: Path) -> None:
    """Even with one pairing declared, which one is seated is written down.

    "The only one" stops being a rule the moment a second is added, and the
    second is added precisely when an operator is restaffing under pressure.
    """

    body = """
[pairings.only.senior]
harness = "claude"
model = "claude-opus-5"

[pairings.only.staff]
harness = "codex"
model = "gpt-5.6-sol"
""".strip()
    cfg = tmp_path / "staffing.toml"
    cfg.write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match="names none as seated"):
        load_bench(cfg)

    cfg.write_text(f'seated_pairing = "nope"\n\n{body}', encoding="utf-8")
    with pytest.raises(ValueError, match="which is not declared"):
        load_bench(cfg)


def test_a_backup_model_must_name_the_harness_that_runs_it(tmp_path: Path) -> None:
    """A bare model id is refused at load, where an operator can still fix it.

    The id alone does not say which CLI runs it, and every reader of this field
    had to assume one. `_replacement_for` assumed the harness of whichever peer
    it found, so a bench with no unspent peer read no backup at all - and an
    outage bench, one vendor in both frontier seats, is precisely that bench.
    """

    cfg = tmp_path / "staffing.toml"
    cfg.write_text(
        """
[bench.junior]
harness = "pi"
model = "gemma4"
backup_models = ["qwen3.8-27b-mtp"]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must name the harness"):
        load_bench(cfg)


def test_a_spent_vendor_moves_the_whole_pair_to_the_fallback_pairing(tmp_path: Path) -> None:
    """The escape is a pairing, so implementer and reviewer move together.

    The per-seat shape this replaced was half a way out twice over: a seat with
    no backup stayed behind on the spent vendor while its pair member escaped,
    and the seat that did move landed beside a reviewer nobody had checked it
    against. Moving to a declared pairing removes both - the landing was
    constructed through `FrontierPairing`, so its two seats are already proven
    distinct.
    """

    body = """
seated_pairing = "claude-pair"

[pairings.claude-pair]
fallback = "codex-pair"

[pairings.claude-pair.senior]
harness = "claude"
model = "claude-opus-5"
reasoning_effort = "max"

[pairings.claude-pair.staff]
harness = "claude"
model = "claude-fable-5"

[pairings.codex-pair.senior]
harness = "codex"
model = "gpt-5.6-sol"
reasoning_effort = "high"

[pairings.codex-pair.staff]
harness = "codex"
model = "gpt-5.6-terra"
reasoning_effort = "high"
""".strip()
    cfg = tmp_path / "staffing.toml"
    cfg.write_text(body, encoding="utf-8")

    staffing = load_staffing(cfg)
    plan = staffing_around_spent_quotas(staffing, frozenset({FrontierHarness.CLAUDE}))
    moved = {item.tier: item for item in plan if item.tier is not Tier.JUNIOR}

    senior, staff = moved[Tier.SENIOR], moved[Tier.STAFF]
    assert isinstance(senior, TierRestaffed) and isinstance(staff, TierRestaffed)
    assert senior.slot.model == "gpt-5.6-sol"
    assert senior.slot.reasoning_effort == "high"
    assert staff.slot.model == "gpt-5.6-terra"
    assert "the pair moves to pairing 'codex-pair'" in senior.detail

    cfg.write_text(
        body.replace(
            'model = "claude-opus-5"',
            'model = "claude-opus-5"\nbackup_models = [{ harness = "codex", model = "x" }]',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="A paired seat escapes with its pair"):
        load_staffing(cfg)


def test_a_spent_vendor_the_pair_does_not_use_moves_nothing(tmp_path: Path) -> None:
    """Codex being spent is not a fact about a pairing seated entirely on claude."""

    cfg = tmp_path / "staffing.toml"
    cfg.write_text(
        """
seated_pairing = "claude-pair"

[pairings.claude-pair.senior]
harness = "claude"
model = "claude-opus-5"

[pairings.claude-pair.staff]
harness = "claude"
model = "claude-fable-5"
""".strip(),
        encoding="utf-8",
    )

    staffing = load_staffing(cfg)
    plan = staffing_around_spent_quotas(staffing, frozenset({FrontierHarness.CODEX}))

    assert all(isinstance(item, TierServed) for item in plan)


def test_a_pair_with_no_viable_fallback_queues_both_seats(tmp_path: Path) -> None:
    """Half a pair is never staffed, in failure exactly as in configuration.

    A fallback whose own vendor is also spent is skipped, and when the chain
    runs out both seats report unstaffable together. Implementing with no
    reviewer standing behind it is the state the pairing type exists to make
    unreachable, so the restaffing does not produce it either.
    """

    cfg = tmp_path / "staffing.toml"
    cfg.write_text(
        """
seated_pairing = "claude-pair"

[pairings.claude-pair]
fallback = "codex-pair"

[pairings.claude-pair.senior]
harness = "claude"
model = "claude-opus-5"

[pairings.claude-pair.staff]
harness = "claude"
model = "claude-fable-5"

[pairings.codex-pair.senior]
harness = "codex"
model = "gpt-5.6-sol"

[pairings.codex-pair.staff]
harness = "codex"
model = "gpt-5.6-terra"
""".strip(),
        encoding="utf-8",
    )

    staffing = load_staffing(cfg)
    plan = staffing_around_spent_quotas(
        staffing, frozenset({FrontierHarness.CLAUDE, FrontierHarness.CODEX})
    )
    frontier = [item for item in plan if item.tier is not Tier.JUNIOR]

    assert len(frontier) == 2
    assert all(isinstance(item, TierUnstaffable) for item in frontier)


def test_a_pairing_cannot_fall_back_to_itself_or_to_nothing(tmp_path: Path) -> None:
    """An escape that names the seating it escapes from is not an escape."""

    cfg = tmp_path / "staffing.toml"
    cfg.write_text(
        """
seated_pairing = "pair"

[pairings.pair]
fallback = "pair"

[pairings.pair.senior]
harness = "claude"
model = "claude-opus-5"

[pairings.pair.staff]
harness = "claude"
model = "claude-fable-5"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="falls back to itself"):
        load_bench(cfg)


def test_the_repo_matrix_survives_the_loss_of_any_one_vendor() -> None:
    """The operator's staffing matrix (2026-08-23), made executable.

    Whichever pairing is seated, killing any single vendor it depends on has to
    land the pair - both seats, together - on a bench that avoids the dead
    vendor and keeps two different models. Not one scenario but the property
    itself, quantified over every pairing the file declares and every vendor
    each depends on: this is what makes editing the config safe, because a
    matrix hole fails here by name instead of surfacing as a queued pipeline
    during the outage that finds it.

    Killing everything a pairing depends on must queue both seats, since the
    both-vendors-out answer is the local ensemble direction and nothing
    declared yet; see docs/local_fallback_seating_gawd.md.
    """

    repo_cfg = Path(__file__).resolve().parents[1] / "configs" / "staffing.toml"
    staffing = load_staffing(repo_cfg)
    accepted_efforts = {None, "low", "medium", "high", "xhigh", "max"}

    for seated in staffing.pairings.values():
        candidate = replace(staffing, seated=seated)
        depends_on = seated.frontier_harnesses()
        for vendor in depends_on:
            plan = staffing_around_spent_quotas(candidate, frozenset({vendor}))
            moved = {item.tier: item for item in plan if item.tier is not Tier.JUNIOR}
            for tier, item in moved.items():
                assert isinstance(item, TierRestaffed), (
                    f"pairing {seated.name!r} has no way off a spent {vendor.value} for "
                    f"its {tier.value} seat; extend its `fallback` chain in "
                    "configs/staffing.toml"
                )
                assert (
                    vendor
                    not in FrontierPairing(
                        name="landed",
                        senior=moved[Tier.SENIOR].slot,
                        staff=moved[Tier.STAFF].slot,
                    ).frontier_harnesses()
                )
                assert item.slot.reasoning_effort in accepted_efforts
            # Constructing the landing as a pairing IS the cross-check assertion:
            # a shared seat would have raised SharedSeatRefused above.

        both_out = staffing_around_spent_quotas(candidate, frozenset(FrontierHarness))
        unstaffed = [item for item in both_out if item.tier is not Tier.JUNIOR]
        assert all(isinstance(item, TierUnstaffable) for item in unstaffed)


def test_the_repo_matrix_is_the_operator_s_matrix() -> None:
    """The three seatings, by name, exactly as ruled on 2026-08-23.

    The property test above proves the matrix has no holes; this one pins its
    content, so a drive-by edit to a model or an effort dial is a deliberate
    act against a named ruling rather than a quiet drift. Changing this test IS
    the act of changing the ruling.
    """

    repo_cfg = Path(__file__).resolve().parents[1] / "configs" / "staffing.toml"
    staffing = load_staffing(repo_cfg)

    def seats(name: str) -> tuple[tuple[str, str | None, str | None], ...]:
        pairing = staffing.pairings[name]
        return tuple(
            (slot.harness.value, slot.model, slot.reasoning_effort)
            for slot in (pairing.senior, pairing.staff)
        )

    assert seats("cross-vendor") == (
        ("codex", "gpt-5.6-sol", "high"),
        ("claude", "claude-opus-5", "xhigh"),
    )
    assert seats("claude-only") == (
        ("claude", "claude-opus-5", "high"),
        ("claude", "claude-fable-5", "high"),
    )
    assert seats("codex-only") == (
        ("codex", "gpt-5.6-terra", "max"),
        ("codex", "gpt-5.6-sol", "high"),
    )
    assert staffing.pairings["cross-vendor"].fallback == ("claude-only", "codex-only")
    assert staffing.pairings["claude-only"].fallback == ("codex-only",)
    assert staffing.pairings["codex-only"].fallback == ("claude-only",)


def test_workload_profile_changes_model_not_seniority_or_capacity(tmp_path: Path) -> None:
    repo_cfg = Path(__file__).resolve().parents[1] / "configs" / "staffing.toml"
    bench = load_bench(repo_cfg)

    standard = resolve_bench_for_workload(
        Tier.SENIOR,
        JudgmentWorkload.STANDARD,
        bench,
    )
    reading = resolve_bench_for_workload(
        Tier.SENIOR,
        JudgmentWorkload.INDEPENDENT_READING,
        bench,
    )

    assert (standard.harness, standard.model, standard.reasoning_effort) == (
        Harness.CODEX,
        "gpt-5.6-sol",
        "high",
    )
    assert (reading.harness, reading.model, reading.reasoning_effort) == (
        Harness.CODEX,
        "gpt-5.6-terra",
        "medium",
    )
    assert reading.capacity == standard.capacity == 2


def test_executor_routes_only_independent_reading_to_its_workload_profile(
    tmp_path: Path,
) -> None:
    repo_cfg = Path(__file__).resolve().parents[1] / "configs" / "staffing.toml"
    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        bench=load_bench(repo_cfg),
    )
    role = JudgmentRole(name="implementer", tier=Tier.SENIOR)
    reading = PowWowTaskSpec(
        task_name="read",
        role="senior independent reader",
        description="inspect the repository",
        purpose=TaskPurpose.ADVISORY,
        judgment=role,
        dispatch_kind="advisory",
        planning_phase=PlanningPhase.SENIOR_INDEPENDENT_READING,
    )
    implementation = PowWowTaskSpec(
        task_name="implement",
        role="senior implementer",
        description="implement the accepted plan",
        purpose=TaskPurpose.IMPLEMENTATION,
        judgment=role,
        dispatch_kind="code",
        planning_phase=PlanningPhase.SENIOR_OWNED_PLAN,
    )

    reading_slot = executor._task_bench_slot(reading)
    implementation_slot = executor._task_bench_slot(implementation)
    assert reading_slot is not None and implementation_slot is not None
    assert (reading_slot.model, reading_slot.reasoning_effort) == (
        "gpt-5.6-terra",
        "medium",
    )
    assert (implementation_slot.model, implementation_slot.reasoning_effort) == (
        "gpt-5.6-sol",
        "high",
    )


def _same_model_config(*, acknowledged: bool) -> str:
    flag = "same_model_review_accepted = true\n" if acknowledged else ""
    return f"""
seated_pairing = "one-model"

[pairings.one-model]
{flag}
[pairings.one-model.senior]
harness = "claude"
model = "claude-opus-5"

[pairings.one-model.staff]
harness = "claude"
model = "claude-opus-5"
""".strip()


def test_one_model_in_both_seats_is_unrepresentable(tmp_path: Path) -> None:
    """The hardening: the reviewer being the author cannot be written down.

    It used to be a log line, and a log line during a 3am outage is not read.
    Then it was a refusal with an acknowledgement flag, and the flag's first use
    was an agent reaching for it past a second model that was already installed.
    Now the pairing does not construct, full stop (operator's ruling,
    2026-08-23): every harness this system staffs offers more than one model,
    so a second model always exists and the last resort the flag served does
    not.
    """

    cfg = tmp_path / "staffing.toml"
    cfg.write_text(_same_model_config(acknowledged=False), encoding="utf-8")

    with pytest.raises(SharedSeatRefused, match="both implementer and reviewer"):
        load_bench(cfg)


def test_the_retired_acknowledgement_flag_is_refused_by_name(tmp_path: Path) -> None:
    """A stale key errors instead of being ignored.

    Silently dropping it would tell an operator their acknowledgement was on
    record when nothing reads it, which is worse than either honoring it or
    refusing it.
    """

    cfg = tmp_path / "staffing.toml"
    cfg.write_text(_same_model_config(acknowledged=True), encoding="utf-8")

    with pytest.raises(ValueError, match="no longer exists"):
        load_bench(cfg)


def test_two_models_from_one_provider_are_a_legal_pairing(tmp_path: Path) -> None:
    """The sanctioned outage seating loads: opus implements, fable reviews.

    The rule is about model identity, not vendor identity. Refusing here would
    make the single-vendor outage seating unwritable, which is the 2026-08-11
    lesson: a rule the operator cannot satisfy gets smuggled past, not obeyed.
    """

    cfg = tmp_path / "staffing.toml"
    cfg.write_text(
        """
seated_pairing = "outage"

[pairings.outage.senior]
harness = "claude"
model = "claude-opus-5"

[pairings.outage.staff]
harness = "claude"
model = "claude-fable-5"
""".strip(),
        encoding="utf-8",
    )

    bench = load_bench(cfg)

    assert bench[Tier.SENIOR].model != bench[Tier.STAFF].model


def test_one_seat_pinned_and_one_defaulted_is_not_decidable_here(tmp_path: Path) -> None:
    """Whether they coincide depends on a CLI default this cannot see.

    Refusing would block a legal seating on a guess; the existing warning made
    the same call, and the pairing keeps its predicate identical rather than
    inventing a second one.
    """

    cfg = tmp_path / "staffing.toml"
    cfg.write_text(
        """
seated_pairing = "maybe"

[pairings.maybe.senior]
harness = "claude"
model = "claude-opus-5"

[pairings.maybe.staff]
harness = "claude"
""".strip(),
        encoding="utf-8",
    )

    assert load_bench(cfg)[Tier.STAFF].model is None


def test_repo_staffing_toml_matches_locked_mapping() -> None:
    """The mapping is locked; the effort dial is not, and pinning both hid that.

    `configs/staffing.toml` locks that both frontier seats hold a frontier
    vendor. Which vendor holds which seat is the operator's call and swapping it
    is expected.

    It no longer demands *two* vendors. `load_bench` already treats two models
    from one provider as the sanctioned outage fallback, and only this assertion
    disagreed - so when one provider's quota went out for six days on
    2026-08-11, the config that the runtime would have accepted could not be
    written down, and the seating had to be smuggled past the file it is
    supposed to be declared in. A rule the code does not enforce and the
    operator cannot satisfy is not protection.

    The property it was reaching for lives in
    `test_the_repo_bench_never_lets_the_reviewer_be_the_author`, which holds
    across any staffing: the reviewer must not be the model that wrote the
    change. That one is unchanged and still refuses a genuinely shared seat.

    `reasoning_effort` is the opposite kind of setting. It is a dial the operator
    turns against cost - senior runs at capacity 3, so effort is paid three times
    on every fan-out - and pinning a specific value here made an intended
    adjustment look like a regression. A tripwire that fires on the changes you
    meant is one you learn to edit without reading, which costs you the ones you
    did not mean.

    So the mapping is asserted exactly and the dial is asserted only to be a
    value the harnesses accept.
    """

    repo_cfg = Path(__file__).resolve().parents[1] / "configs" / "staffing.toml"
    bench = load_bench(repo_cfg)
    senior = bench[Tier.SENIOR]
    staff = bench[Tier.STAFF]

    assert {senior.harness, staff.harness} <= {Harness.CLAUDE, Harness.CODEX}, (
        "both frontier seats must be staffed by a frontier vendor; the junior harness "
        "in a frontier seat is a different mistake and this is where it shows"
    )
    assert bench[Tier.JUNIOR].harness == Harness.PI
    # `xhigh` joined this set on 2026-08-13, probed against the claude CLI
    # (`claude --model claude-opus-5 --effort xhigh` answers) before being
    # written here. The set is the harnesses' accepted vocabulary and nothing
    # more; it is widened by proving a value, never by assuming one.
    accepted_efforts = {"low", "medium", "high", "xhigh", "max"}
    assert senior.reasoning_effort in accepted_efforts
    assert staff.reasoning_effort in accepted_efforts


def test_the_repo_bench_never_lets_the_reviewer_be_the_author() -> None:
    """The invariant the seating exists to serve, stated on its own.

    Separate from the mapping test because it survives any future swap: whatever
    sits in the two seats, review is worth running only if the reviewer can
    disagree with the implementer for reasons the implementer did not already
    have. `load_bench` warns when this is violated; this refuses to let the
    repository's own config be the violation.
    """

    repo_cfg = Path(__file__).resolve().parents[1] / "configs" / "staffing.toml"
    bench = load_bench(repo_cfg)
    senior = bench[Tier.SENIOR]
    staff = bench[Tier.STAFF]

    assert (senior.harness, senior.model) != (staff.harness, staff.model)


def test_prose_that_names_the_seating_matches_the_config() -> None:
    """The find-and-replace list, enforced instead of remembered.

    Most prose is deliberately seat-generic after the 2026-08-09 swap left four
    stale claims behind, one of them a sentence the demo operator reads on
    camera. Three places still name the vendor because their reader needs the
    word: the demo narration, the README's as-staffed-today sentence, and the
    config's own header. Each is asserted here against the loaded bench, so
    editing staffing.toml fails this test with the exact sentences to update -
    which is the mechanism for keeping prose coupled to config: not a habit of
    searching, but a test that names every coupled location when it fires.

    Anything this test does not assert is supposed to be seat-generic; a new
    vendor-per-seat sentence anywhere else should either join this list or lose
    the vendor name.
    """

    repo_root = Path(__file__).resolve().parents[1]
    bench = load_bench(repo_root / "configs" / "staffing.toml")
    senior = bench[Tier.SENIOR].harness
    staff = bench[Tier.STAFF].harness
    spoken = {Harness.CLAUDE: "Claude", Harness.CODEX: "Codex"}
    written = {Harness.CLAUDE: "Claude Code", Harness.CODEX: "Codex"}

    # The shooting script is private-repo-owned: it is a recording plan, it names
    # what not to show on camera, and publishing it would tell a viewer which
    # parts of the video were pre-recorded. So it is checked when present rather
    # than shipped to keep this assertion reachable.
    demo_path = repo_root / "docs" / "demo_shooting_script.md"
    if demo_path.exists():
        demo = demo_path.read_text(encoding="utf-8")
        assert f"implementation was {spoken[senior]}, review is {spoken[staff]}" in demo, (
            "the demo's 3:30 narration names the wrong vendor for the seat; "
            "update docs/demo_shooting_script.md to match configs/staffing.toml"
        )

    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    assert f"that is {written[senior]} implementing and {written[staff]} reviewing" in readme, (
        "the README's as-staffed-today sentence disagrees with configs/staffing.toml; "
        "update the Frontier CLIs section"
    )

    header = (repo_root / "configs" / "staffing.toml").read_text(encoding="utf-8")
    assert f"# Locked mapping: {senior.value} = senior, {staff.value} = staff" in header, (
        "staffing.toml's own header comment disagrees with the tables below it"
    )


def test_roster_payload_is_json_friendly() -> None:
    payload = Roster(
        judgment=(JudgmentRole("implementer", Tier.SENIOR),),
        checks=(CheckRole("test_runner", "uv run pytest"),),
    ).to_payload()
    judgment = cast(list[dict[str, Any]], payload["judgment"])
    checks = cast(list[dict[str, Any]], payload["checks"])
    assert judgment[0]["tier"] == "senior"
    assert checks[0]["command"] == "uv run pytest"


def test_bench_slot_reasoning_effort_reaches_codex_command(tmp_path) -> None:
    from pathlib import Path

    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import load_bench

    config = tmp_path / "staffing.toml"
    config.write_text(
        """
seated_pairing = "pair"

[pairings.pair.senior]
harness = "claude"
model = "claude-opus-5"

[pairings.pair.staff]
harness = "codex"
model = "gpt-5.6-sol"
reasoning_effort = "high"
capacity = 1
""",
        encoding="utf-8",
    )
    bench = load_bench(Path(config))
    executor = CliPowWowExecutor(worktree_root=tmp_path / "wt", bench=bench)
    command = executor._build_agent_cli_command(
        FrontierHarness.CODEX,
        "gpt-5.6-sol",
        "review this",
        ReadOnlyInspection(),
        reasoning_effort="high",
    )
    assert "--model" in command and "gpt-5.6-sol" in command
    assert "-c" in command and "model_reasoning_effort=high" in command
    assert "-s" in command and "read-only" in command


def test_bench_slot_reasoning_effort_reaches_claude_command(tmp_path: Path) -> None:
    from local_first_agent_os.pow_wow import CliPowWowExecutor

    executor = CliPowWowExecutor(worktree_root=tmp_path / "wt")
    command = executor._build_agent_cli_command(
        FrontierHarness.CLAUDE,
        None,
        "implement this",
        UnattendedImplementation(),
        reasoning_effort="max",
    )
    assert "--effort" in command
    assert command[command.index("--effort") + 1] == "max"
