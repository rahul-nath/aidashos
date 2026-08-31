# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Every model id a dispatch could spawn has to be one the probe asked about.

`frontier_probe.py` exists because a model id that no longer answers otherwise
surfaces as a dispatch failure mid-run, on the operator's quota, spending one of
a milestone's three attempts to learn what one nonce completion would have said
at startup. It then looked at `bench.items()` and probed `slot.model`, which is
one model per tier.

A tier is not one model. `resolve_bench_for_workload` swaps in a workload
profile's id, so the senior seat spawns `gpt-5.6-sol` for implementation and a
different id for independent reading. That second id was never asked anything:
`gpt-5.6-terra` was configured on 2026-08-16 and on 2026-08-17 nothing in the
repository had ever checked that it exists. Nothing validates ids at load, so the
first thing to find out would have been a dispatch.

Both probes now enumerate through `staffing.spawnable_models`. These tests pin
that, and the first one deliberately re-derives the answer from
`workload_profiles` directly so the guard is a second opinion rather than a
restatement of the implementation.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from staffing_support import repo_bench

from local_first_agent_os import frontier_probe
from local_first_agent_os.staffing import (
    Bench,
    BenchSlot,
    FrontierHarness,
    Harness,
    JudgmentWorkload,
    WorkloadModelProfile,
    classify_harness,
    spawnable_models,
)
from local_first_agent_os.vocabulary import DispatchTier


def _declared_model_ids(bench: Bench) -> set[tuple[Harness, str | None]]:
    """Every (harness, model) the config names, read straight off the slots."""

    declared: set[tuple[Harness, str | None]] = set()
    for slot in bench.values():
        declared.add((slot.harness, slot.model))
        for profile in slot.workload_profiles:
            declared.add((slot.harness, profile.model))
    return declared


def test_every_model_the_repo_staffs_is_offered_to_the_probe() -> None:
    """The guard for the real gap, against the real `configs/staffing.toml`.

    Reading `workload_profiles` directly here is the point. `spawnable_models`
    resolves through `resolve_bench_for_workload`, and a test that enumerated the
    same way would agree with it no matter what either did.
    """

    bench = repo_bench()

    offered = {(item.harness, item.model) for item in spawnable_models(bench)}

    assert _declared_model_ids(bench) <= offered, (
        "configs/staffing.toml names a model the readiness probe would never ask "
        "about. An id nothing proves is an id a dispatch discovers, mid-run, on "
        "quota."
    )


def test_a_workload_profile_is_offered_under_its_own_name() -> None:
    """Two ids on one seat need two labels, or one failure reads as the other."""

    bench: Bench = {
        DispatchTier.SENIOR: BenchSlot(
            harness=Harness.CODEX,
            model="base-model",
            workload_profiles=(
                WorkloadModelProfile(
                    workload=JudgmentWorkload.INDEPENDENT_READING,
                    model="reading-model",
                ),
            ),
        )
    }

    offered = spawnable_models(bench)

    assert [item.model for item in offered] == ["base-model", "reading-model"]
    assert offered[0].label == "senior (codex base-model)"
    assert offered[1].label == "senior[independent_reading] (codex reading-model)"


def test_a_profile_that_only_changes_effort_is_not_probed_twice() -> None:
    """Same id, same answer. A second completion buys nothing and costs quota."""

    bench: Bench = {
        DispatchTier.SENIOR: BenchSlot(
            harness=Harness.CODEX,
            model="one-model",
            reasoning_effort="high",
            workload_profiles=(
                WorkloadModelProfile(
                    workload=JudgmentWorkload.INDEPENDENT_READING,
                    model="one-model",
                    reasoning_effort="medium",
                ),
            ),
        )
    }

    assert [item.model for item in spawnable_models(bench)] == ["one-model"]


def test_a_new_workload_needs_no_edit_here_to_be_covered() -> None:
    """Enumerating over `JudgmentWorkload` is what makes this total.

    A member added to that enum reaches `spawnable_models` on the day it is
    added, because the resolver is asked about every member rather than about a
    list somebody remembered to extend.
    """

    bench: Bench = {DispatchTier.SENIOR: BenchSlot(harness=Harness.CODEX, model="only")}

    covered = {item.workload for item in spawnable_models(bench)}
    # One id, so one entry survives dedup; the enumeration still visited each.
    assert covered <= set(JudgmentWorkload)
    assert JudgmentWorkload.STANDARD in covered


def test_the_startup_probe_asks_about_every_spawnable_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`start-agent-runtime.sh` runs this on every start, so it is the one that matters.

    Asserted on the argv the probe would actually run, not on its return value: a
    proof that never spawned the command is what the previous version produced
    for a workload profile, and it looked identical to success.
    """

    bench: Bench = {
        DispatchTier.SENIOR: BenchSlot(
            harness=Harness.CODEX,
            model="senior-model",
            workload_profiles=(
                WorkloadModelProfile(
                    workload=JudgmentWorkload.INDEPENDENT_READING,
                    model="reader-model",
                ),
            ),
        ),
        DispatchTier.STAFF: BenchSlot(harness=Harness.CLAUDE, model="staff-model"),
        # Local, and skipped: there is no CLI to ask.
        DispatchTier.JUNIOR: BenchSlot(harness=Harness.PI, model="gemma4"),
    }
    asked: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> Any:
        asked.append(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(frontier_probe.subprocess, "run", fake_run)

    proofs = frontier_probe.probe_bench(bench)

    probed = {command[command.index("--model") + 1] for command in asked}
    assert probed == {"senior-model", "reader-model", "staff-model"}
    assert all(proof.proved for proof in proofs)
    assert "gemma4" not in probed


def test_the_probe_still_skips_a_local_harness() -> None:
    """The junior tier proves itself against the model server, not through a CLI."""

    bench: Bench = {DispatchTier.JUNIOR: BenchSlot(harness=Harness.PI, model="gemma4")}

    frontier = [
        item
        for item in spawnable_models(bench)
        if isinstance(classify_harness(item.harness), FrontierHarness)
    ]

    assert frontier == []
