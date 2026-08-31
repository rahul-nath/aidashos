# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Probe each staffed frontier model without deciding staffing or writing state."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .settings import get_settings
from .staffing import (
    Bench,
    FrontierHarness,
    classify_harness,
    load_bench,
    spawnable_models,
)
from .vocabulary import DispatchTier

_NONCE = "Reply with exactly: ok"
_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class TierProof:
    """What one staffed frontier tier answered, and what it was asked."""

    tier: DispatchTier
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


_NEUTRAL_PROBE_DIR = Path(tempfile.gettempdir()) / "local-agent-nonce-probe"


def probe_model(harness: FrontierHarness, model: str | None) -> tuple[bool, str | None]:
    """Ask one model whether it can accept work. Returns ``(alive, detail)``.

    The `ProbeFn` the pairing lattice walks. It answers about one model rather
    than a whole bench because a model appears in many pairings and the walk
    must not ask it once per pairing.

    Run from an empty directory rather than from the repository. `codex exec`
    reads `AGENTS.md` out of its working directory, which accounted for 945 of
    the 13,660 tokens a probe from the repo root spent when this was measured on
    2026-08-30. The remaining ~12.7k is the CLI's own system prompt and tool
    schemas, sent on every invocation whatever the message says, so that part is
    a floor rather than something this function can economise on. It is still
    far cheaper than the alternative it replaces, which was benching a working
    provider for five hours.
    """

    _NEUTRAL_PROBE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            nonce_command(harness, model),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            cwd=_NEUTRAL_PROBE_DIR,
        )
    except FileNotFoundError:
        return False, f"the {harness.value} CLI is not installed"
    except subprocess.TimeoutExpired:
        return False, f"no answer within {_TIMEOUT_SECONDS}s"
    if completed.returncode == 0:
        return True, None
    lines = (completed.stderr.strip() or completed.stdout.strip()).splitlines()
    return False, lines[-1] if lines else f"exit {completed.returncode}"


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
    """Tell the operator which configured frontier models cannot accept work."""

    lines = [
        f"Frontier tier answered readiness proof: {proof.label}."
        for proof in proofs
        if proof.proved
    ]
    unproved = [proof for proof in proofs if not proof.proved]
    lines.extend(
        f"WARNING: frontier tier did not answer its readiness proof - {proof.label}: {proof.detail}"
        for proof in unproved
    )
    if unproved:
        lines.append(
            "         A dispatch to an unproved tier records a charged failure "
            "against the milestone's retry policy."
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
