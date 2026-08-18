# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prove, at startup, that each staffed frontier tier can answer at all.

The junior tier already answers a readiness proof before the runtime finishes
starting, and the frontier tiers did not. That asymmetry is what let a spent
subscription stay invisible until a milestone discovered it: `first-run-check.sh
--probe-frontier-models` existed but was opt-in and manual, and the dispatch-time
check reads *history* - ledger rows for recent usage-limit failures - so a
provider that died longer ago than its five-hour cooldown looks available.

That is not hypothetical. On 2026-08-11 the codex CLI was exhausted for six days
while its last failure row aged out of the window, and each milestone that
reached for it spent one of its three attempts learning what one nonce
completion would have said at startup.

This does not decide staffing and does not write to the ledger. It asks each
staffed frontier tier one question and reports the answer, which is the cheapest
evidence there is that the next dispatch can run.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .settings import get_settings
from .staffing import (
    Bench,
    FrontierHarness,
    Tier,
    classify_harness,
    load_bench,
    spawnable_models,
)

_NONCE = "Reply with exactly: ok"
_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class TierProof:
    """What one staffed frontier tier answered, and what it was asked."""

    tier: Tier
    label: str
    proved: bool
    detail: str | None = None


def nonce_command(kind: FrontierHarness, model: str | None) -> list[str]:
    """The one-line completion this harness answers, in its own dialect.

    Kept identical in shape to `first-run-check.sh`'s probe on purpose: an
    operator proving a candidate id by hand before writing it into
    `configs/staffing.toml` should be running the same command this does.
    """

    if kind is FrontierHarness.CLAUDE:
        command = ["claude", "--print"]
    else:
        command = ["codex", "exec", "--skip-git-repo-check"]
    if model:
        command += ["--model", model]
    command.append(_NONCE)
    return command


def probe_bench(bench: Bench) -> tuple[TierProof, ...]:
    """Ask every model a dispatch could spawn its one question.

    Every model, not every tier. This used to walk `bench.items()` and probe
    `slot.model`, which proved the standard workload and silently skipped the
    rest: a seat with an `independent_reading` profile spawns a different id for
    reading tasks, and that id was never asked anything. `gpt-5.6-terra` sat in
    `configs/staffing.toml` unproved from the day it was configured. The
    enumeration now comes from `staffing.spawnable_models`, which resolves
    through the same function dispatch uses.

    Local tiers are skipped rather than reported as unproved: the junior tier
    answers its own readiness proof against the model server, and a harness with
    no CLI to spawn has nothing to ask here.
    """

    proofs: list[TierProof] = []
    for spawnable in spawnable_models(bench):
        kind = classify_harness(spawnable.harness)
        if not isinstance(kind, FrontierHarness):
            continue
        tier, label = spawnable.tier, spawnable.label
        try:
            completed = subprocess.run(
                nonce_command(kind, spawnable.model),
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            proofs.append(
                TierProof(tier, label, False, f"the {spawnable.harness.value} CLI is not installed")
            )
            continue
        except subprocess.TimeoutExpired:
            proofs.append(TierProof(tier, label, False, f"no answer within {_TIMEOUT_SECONDS}s"))
            continue
        if completed.returncode == 0:
            proofs.append(TierProof(tier, label, True))
            continue
        lines = (completed.stderr.strip() or completed.stdout.strip()).splitlines()
        detail = lines[-1] if lines else f"exit {completed.returncode}"
        proofs.append(TierProof(tier, label, False, detail))
    return tuple(proofs)


def report(proofs: tuple[TierProof, ...]) -> str:
    """What the operator reads, including what an unproved tier will cost.

    The consequence is spelled out rather than left to be inferred. "codex
    reported a usage limit" is a fact; "a dispatch to this tier fails and spends
    one of its milestone's attempts" is the reason to act on it before starting
    work rather than after.
    """

    lines = [
        f"Frontier tier answered readiness proof: {proof.label}."
        for proof in proofs
        if proof.proved
    ]
    unproved = [proof for proof in proofs if not proof.proved]
    lines.extend(
        f"WARNING: frontier tier did not answer its readiness proof - "
        f"{proof.label}: {proof.detail}"
        for proof in unproved
    )
    if unproved:
        lines.append(
            "         A dispatch to an unproved tier fails and spends one of its "
            "milestone's attempts."
        )
        lines.append(
            "         Restaff the seat in configs/staffing.toml, or wait out the "
            "window, before starting work."
        )
    return "\n".join(lines)


def main() -> int:
    """Always zero.

    A spent subscription is a state an operator works around - restaffing the
    seat, or waiting out a window - and the cockpit, the ledger and the resident
    loops are the tools they would use to do it. Refusing to finish starting the
    runtime would take those away over a condition the runtime itself is fine
    with. The junior preload does exit non-zero, because nothing downstream works
    without it; this one is a warning by design.
    """

    print(report(probe_bench(load_bench(get_settings().config_dir / "staffing.toml"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
