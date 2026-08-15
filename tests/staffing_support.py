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

from local_first_agent_os.staffing import Bench, Harness, Tier, load_bench

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
