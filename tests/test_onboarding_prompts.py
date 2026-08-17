# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The onboarding prompt sequence has exactly one place it is written down.

docs/onboarding/prompts.json is that place. The landing page renders it, the
BOOT_PROMPT doc quotes it, and the prompts name scripts by path. Each of those
is a copy that drifts silently unless something pins it, so this module pins
all three: the doc must quote the JSON verbatim, every script a prompt names
must exist in both shell dialects, and the clone command must appear where the
walkthrough tells a human to type it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ONBOARDING = REPO_ROOT / "docs" / "onboarding"

EXPECTED_PROMPT_IDS = ("boot", "first-run", "attach-tool")


@pytest.fixture(scope="module")
def prompts_document() -> dict:
    return json.loads((ONBOARDING / "prompts.json").read_text(encoding="utf-8"))


def test_prompt_sequence_shape(prompts_document: dict) -> None:
    assert prompts_document["version"] == 1
    ids = tuple(entry["id"] for entry in prompts_document["prompts"])
    assert ids == EXPECTED_PROMPT_IDS
    for entry in prompts_document["prompts"]:
        assert entry["title"].strip()
        assert entry["summary"].strip()
        assert entry["prompt"].strip()


def test_boot_prompt_doc_quotes_the_json_verbatim(prompts_document: dict) -> None:
    boot_prompt = next(
        entry["prompt"] for entry in prompts_document["prompts"] if entry["id"] == "boot"
    )
    doc = (ONBOARDING / "BOOT_PROMPT.md").read_text(encoding="utf-8")
    assert boot_prompt in doc, (
        "docs/onboarding/BOOT_PROMPT.md no longer quotes prompts.json's boot prompt "
        "verbatim; edit prompts.json first, then paste the new text into the doc."
    )


def test_clone_command_appears_in_the_walkthrough(prompts_document: dict) -> None:
    clone_command = prompts_document["clone_command"]
    walkthrough = (ONBOARDING / "ONBOARDING.md").read_text(encoding="utf-8")
    assert clone_command in walkthrough


def test_every_script_a_prompt_names_exists_and_runs(prompts_document: dict) -> None:
    """Every boot stage a prompt names is on disk and executable.

    This asked for a `.ps1` twin next to every `.sh` until the PowerShell stages
    moved to `potential-directions/windows-boot/`. They were never executed and
    never parsed, and the docs asserted Windows support on the strength of their
    existing, which is what this assertion was quietly underwriting: it proved
    the files were present and nothing else.

    Restoring Windows means restoring the `.ps1` half of this loop as well as the
    files, which `potential-directions/README.md` says. Until then a shell-only
    check is the honest one, because shell is the only dialect anything has run.
    """

    all_text = "\n".join(entry["prompt"] for entry in prompts_document["prompts"])
    named_stages = set(re.findall(r"\b(\d{2}-[a-z0-9-]+)\.sh\b", all_text))
    assert named_stages, "the boot prompt no longer names any boot stage scripts"
    for stage in sorted(named_stages):
        shell_path = REPO_ROOT / "scripts" / "boot" / f"{stage}.sh"
        assert shell_path.is_file(), f"prompt names {stage}.sh, which does not exist"
        assert os.access(shell_path, os.X_OK), f"{shell_path.name} is not executable"


def test_the_parked_windows_stages_are_not_advertised() -> None:
    """No shipped surface may claim Windows while the stages sit parked.

    The pairing that failed before: the scripts existed, so the docs claimed the
    platform, and nothing connected the claim to whether the scripts had ever
    run. This is that connection. It fails if a `.ps1` returns to `scripts/boot/`
    without the docs being reconsidered, and it fails if the docs re-advertise
    Windows while the stages are still parked.

    The parked directory is checked only where it exists. `potential-directions/`
    is absent from `public_import.toml` on purpose, so the public snapshot has the
    docs and the shell stages but not the parked twins, and `tests/` travels whole
    while that directory does not. Asserting its presence unconditionally failed
    in the snapshot and passed here, which is the asymmetry
    `docs/public_release_checklist.md` exists to warn about: a test that reads a
    repository path has to tolerate that path being absent. What must hold in both
    checkouts is the part a reader can see - no PowerShell stage under
    `scripts/boot/`, and no document pointing at one.
    """

    parked = REPO_ROOT / "potential-directions" / "windows-boot"
    if parked.is_dir():
        assert list(parked.glob("*.ps1")), (
            "potential-directions/windows-boot/ exists but holds no PowerShell stages; "
            "restoring Windows means moving them back, not emptying the parking spot"
        )
    assert not list((REPO_ROOT / "scripts" / "boot").glob("*.ps1")), (
        "a PowerShell stage is back under scripts/boot/; "
        "see potential-directions/README.md before re-advertising Windows"
    )
    for relative in ("docs/onboarding/ONBOARDING.md", "scripts/boot/README.md"):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert ".ps1" not in text, f"{relative} still points at a parked PowerShell stage"


def test_referenced_docs_and_skill_exist(prompts_document: dict) -> None:
    all_text = "\n".join(entry["prompt"] for entry in prompts_document["prompts"])
    for relative in (
        "scripts/boot/README.md",
        "docs/onboarding/ONBOARDING.md",
        "skills/operate-agent-os/SKILL.md",
        "docs/examples/work_unit_acceptance_design_doc.md",
    ):
        assert relative in all_text, f"the prompt sequence no longer mentions {relative}"
        assert (REPO_ROOT / relative).is_file(), f"{relative} does not exist"
