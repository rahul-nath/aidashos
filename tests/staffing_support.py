# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which vendor holds which seat, read from the file the runtime reads.

A test that spawns a fake agent, or names one in a grant, has to name a vendor,
and the only thing that decides what that vendor may do is the seat it holds in
`configs/staffing.toml`. `capability_gate.policy_principal` resolves an agent's
vendor name to that seat and checks `POLICIES.md` against the seat, so a test
that guesses the seating names a principal the gate will refuse - an implementer
called by the reviewer's vendor is denied `run_command`, correctly, for a reason
none of those tests is about.

`DEFAULT_BENCH` is the wrong source for that name. It is the fallback for a
deployment with no staffing file, deliberately not equal to the repo config (see
its comment in `staffing.py`), and the two have disagreed about which vendor is
senior. Reading it made a dozen tests encode one seating and fail the day an
operator changed it, which is the modular seat this repo built being broken by a
static rule in its own suite.

So the seat is asked here, once, from the same config the gate loads. Every
helper is seat-generic: nothing below names a vendor, and a reseating changes
what these return without changing a line of test code.
"""

from __future__ import annotations

from pathlib import Path

from local_first_agent_os.staffing import Bench, FrontierPairing, Harness, Tier, load_bench

REPO_STAFFING_CONFIG = Path(__file__).resolve().parent.parent / "configs" / "staffing.toml"


def repo_bench() -> Bench:
    """The staffing the runtime resolves principals against."""

    return load_bench(REPO_STAFFING_CONFIG)


def seat_vendor(tier: Tier) -> Harness:
    """The harness seated at `tier` by the repo's staffing config."""

    return repo_bench()[tier].harness


def seat_agent_name(tier: Tier) -> str:
    """The agent name a fake filling `tier`'s seat must answer to."""

    return seat_vendor(tier).value


def two_vendor_bench() -> Bench:
    """A fixed cross-vendor seating, for tests whose premise IS two vendors.

    The repo's config may seat one vendor in both frontier seats (an outage
    staffing, sanctioned by `load_bench`). Most tests should inherit whatever
    the config says, per this module's docstring - but a test about
    cross-vendor mechanics (provider fallback, the vendor cross-check) asserts
    vendor pairs by name, and inheriting a same-vendor seating deletes its
    subject. Such a test declares this seating explicitly instead. The shape is
    the historical one: codex implements, claude reviews. Models are cleared
    because they belong to the config's seating, not this synthetic one.

    Built through `FrontierPairing` rather than by editing two slots, so this
    helper is held to the rule the config is held to. A synthetic seating is
    still a seating, and one that quietly put a vendor in both seats would hand
    every cross-vendor test a bench with no cross-vendor in it.
    """

    from dataclasses import replace

    bench = dict(repo_bench())
    pairing = FrontierPairing(
        name="two_vendor_bench",
        senior=replace(
            bench[Tier.SENIOR],
            harness=Harness.CODEX,
            model=None,
            backup_models=(),
            workload_profiles=(),
        ),
        staff=replace(
            bench[Tier.STAFF],
            harness=Harness.CLAUDE,
            model=None,
            backup_models=(),
            workload_profiles=(),
        ),
    )
    bench.update(pairing.seats())
    return bench
