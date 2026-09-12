# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from local_first_agent_os import pairing_assignment as assignments
from local_first_agent_os.pairing_lattice import ProbeCache
from local_first_agent_os.staffing import Harness, load_staffing

REPO = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path, policy: str) -> Path:
    path = tmp_path / "staffing.toml"
    path.write_text(
        'seated_pairing = "sol"\n'
        '[pairings.sol]\nfallback = ["alternative"]\n'
        '[pairings.sol.senior]\nharness = "codex"\n'
        'model = "gpt-5.6-sol"\nreasoning_effort = "xhigh"\n'
        '[pairings.sol.staff]\nharness = "codex"\n'
        'model = "gpt-5.6-sol"\nreasoning_effort = "max"\n'
        '[pairings.alternative.senior]\nharness = "codex"\nmodel = "gpt-5.6-sol"\n'
        '[pairings.alternative.staff]\nharness = "claude"\nmodel = "claude-opus-5"\n' + policy,
        encoding="utf-8",
    )
    return path


def _select(path: Path, *, work_unit_id: str = "wu", attempt: int = 1):
    return assignments.select_assignment(
        work_unit_id=work_unit_id,
        milestone_key="implement",
        attempt=attempt,
        chart_path=REPO / "configs/model_quality.toml",
        staffing_path=path,
        moment=datetime(2026, 9, 3, tzinfo=UTC),
        cache=ProbeCache(),
    )


def test_fixed_pair_survives_ranking_and_never_probes_another_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, '[work_unit_pairing]\nmode = "fixed"\npairing = "sol"\n')
    asked = []

    def probe(harness, model, **_kwargs):
        asked.append((harness, model))
        return True, None

    monkeypatch.setattr(assignments, "recent_dispatch_probe", probe)
    result = _select(path)
    assert result.pairing.senior.model == result.pairing.staff.model == "gpt-5.6-sol"
    assert result.pairing.senior.reasoning_effort == "xhigh"
    assert result.pairing.staff.reasoning_effort == "max"
    assert asked == [(Harness.CODEX, "gpt-5.6-sol")]
    assert result.to_payload()["selection_policy"] == {"mode": "fixed", "pairing": "sol"}
    assert assignments.PairingAssignment.from_payload(result.to_payload()) == result


def test_fixed_pair_fails_closed_even_with_a_declared_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, '[work_unit_pairing]\nmode = "fixed"\npairing = "sol"\n')
    asked = []

    def probe(harness, model, **_kwargs):
        asked.append((harness, model))
        return harness is not Harness.CODEX, "quota exhausted"

    monkeypatch.setattr(assignments, "recent_dispatch_probe", probe)
    with pytest.raises(assignments.NoLivePairing, match="quota exhausted"):
        _select(path)
    assert asked == [(Harness.CODEX, "gpt-5.6-sol")]


def test_work_unit_override_does_not_change_other_work_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(
        tmp_path,
        '[work_unit_pairing]\nmode = "auto"\n'
        '[work_unit_pairing_overrides.wu]\nmode = "fixed"\npairing = "sol"\n',
    )
    monkeypatch.setattr(assignments, "recent_dispatch_probe", lambda *args, **kw: (True, None))
    assert _select(path).pairing.staff.harness is Harness.CODEX
    assert _select(path, work_unit_id="another").pairing.staff.harness is Harness.CLAUDE


@pytest.mark.parametrize(
    "policy",
    [
        '[work_unit_pairing]\nmode = "typo"\n',
        '[work_unit_pairing]\nmode = "fixed"\n',
        '[work_unit_pairing]\nmode = "fixed"\npairing = "missing"\n',
        '[work_unit_pairing]\nmode = "auto"\npairing = "sol"\n',
        '[work_unit_pairing]\nmode = "fixed"\npairing = "sol"\nfallbak = true\n',
        'work_unit_pairing = "auto"\n',
        '[work_unit_pairing_overrides.wu]\nmode = "fixed"\npairing = "missing"\n',
    ],
)
def test_invalid_selection_is_refused_at_config_load(tmp_path: Path, policy: str) -> None:
    # Root scalar syntax must precede the pairing tables, not land in a staff seat.
    path = _config(tmp_path, policy if policy.startswith("[") else "")
    if not policy.startswith("["):
        path.write_text(policy + path.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="pairing"):
        load_staffing(path)


def test_fixed_pair_requires_an_exact_chart_entry_before_any_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, '[work_unit_pairing]\nmode = "fixed"\npairing = "sol"\n')
    path.write_text(path.read_text(encoding="utf-8").replace('"xhigh"', '"typo"'))

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid selection reached availability probing")

    monkeypatch.setattr(assignments, "recent_dispatch_probe", forbidden)
    with pytest.raises(ValueError, match="chart"):
        _select(path)


def test_repo_default_is_sol_xhigh_implementation_and_astra_high_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assignments, "recent_dispatch_probe", lambda *args, **kw: (True, None))
    result = _select(REPO / "configs/staffing.toml")
    assert result.pairing.senior.label == "codex:gpt-5.6-sol@xhigh"
    assert result.pairing.staff.label == "codex:gpt-6-astra@high"


def test_missing_explicit_config_cannot_silently_enable_auto_ranking(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="pairing configuration is missing"):
        _select(tmp_path / "missing.toml")


def test_legacy_auto_assignment_round_trips_without_rewriting_the_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, "")
    monkeypatch.setattr(assignments, "recent_dispatch_probe", lambda *args, **kw: (True, None))
    payload = _select(path).to_payload()
    assert "selection_policy" not in payload
    assert assignments.PairingAssignment.from_payload(payload).to_payload() == payload
