# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from local_first_agent_os import pairing_assignment as assignments
from local_first_agent_os.coordination.dispatch import submit_dispatch_intent
from local_first_agent_os.coordination.store import rowdict, tx
from local_first_agent_os.pairing_lattice import ProbeCache
from local_first_agent_os.pairing_resolution import resolve_assignment
from local_first_agent_os.staffing import Harness, load_staffing

ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path, policy: str) -> Path:
    path = tmp_path / "staffing.toml"
    path.write_text(
        re.sub(
            r"\[work_unit_pairing\]\n.*?(?=\n# Optional)",
            "[work_unit_pairing]\n" + policy + "\n",
            (ROOT / "configs/staffing.toml").read_text(),
            flags=re.S,
        )
    )
    return path


def _resolve(path: Path, attempt: int = 1):
    return resolve_assignment(
        work_unit_id="wu-resolution",
        milestone_key="implement",
        attempt=attempt,
        chart_path=ROOT / "configs/model_quality.toml",
        staffing_path=path,
        moment=datetime(2026, 9, 3, tzinfo=UTC),
        cache=ProbeCache(),
    )


def _events():
    with tx() as c:
        rows = c.execute(
            "SELECT payload_json FROM ledger_events "
            "WHERE aggregate_type='pairing_resolution' ORDER BY event_id"
        ).fetchall()
    return [json.loads(str(rowdict(row)["payload_json"])) for row in rows]


PREFERRED = (
    'mode="preferred"\npairing="codex-only"\nfallback={mode="explicit", pairings=["claude-only"]}'
)


def test_preferred_quota_fallback_records_intent_then_actual_actors(
    work_unit_ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, PREFERRED)
    monkeypatch.setattr(
        assignments,
        "recent_dispatch_probe",
        lambda harness, model, **kw: (harness is Harness.CLAUDE, "usage limited until tomorrow"),
    )
    result = _resolve(path)
    events = _events()
    assert [item["kind"] for item in events] == [
        "pairing_requested",
        "availability_rejected",
        "availability_rejected",
        "fallback_selected",
        "pairing_resolved",
    ]
    assert events[0]["selection_policy"]["pairing"] == "codex-only"
    assert events[1]["reason"] == "usage limited until tomorrow"
    assert [event["model"] for event in events[1:3]] == ["gpt-5.6-sol", "gpt-6-astra"]
    assert events[3]["senior"]["model"] == "claude-sonnet-5"
    assert events[-1]["assignment"] == result.to_payload()
    assert result.pairing.senior.harness is result.pairing.staff.harness is Harness.CLAUDE
    assert result.resolution_id
    assert [item["sequence"] for item in events] == list(range(1, 6))
    with tx() as c:
        assert c.execute("SELECT count(*) AS n FROM dispatch_intents").fetchone()["n"] == 0

    # A crash between resolution and enqueue cannot consult changed policy.
    path.write_text("invalid TOML!")
    monkeypatch.setattr(
        assignments, "recent_dispatch_probe", lambda *a, **kw: pytest.fail("reprobe")
    )
    assert _resolve(path) == result
    submitted = submit_dispatch_intent(
        "senior",
        "implement",
        kind="code",
        idempotency_key="resolved-test",
        pairing_assignment=result.to_payload(),
    )
    assert assignments.assignment_for_intent(submitted["intent_id"]) == result
    assert _events() == events


def test_preferred_pair_wins_even_if_a_fallback_scores_higher(
    work_unit_ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, PREFERRED)
    monkeypatch.setattr(assignments, "recent_dispatch_probe", lambda *a, **kw: (True, None))
    result = _resolve(path)
    assert result.pairing.senior.label == "codex:gpt-5.6-sol@xhigh"
    assert result.pairing.staff.label == "codex:gpt-6-astra@high"
    assert [event["kind"] for event in _events()] == ["pairing_requested", "pairing_resolved"]


@pytest.mark.parametrize("policy", ['mode="fixed"\npairing="codex-only"', PREFERRED])
def test_exhaustion_is_durable_and_replay_does_not_reselect(
    work_unit_ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    path = _config(tmp_path, policy)
    monkeypatch.setattr(assignments, "recent_dispatch_probe", lambda *a, **kw: (False, "quota"))
    with pytest.raises(assignments.NoLivePairing):
        _resolve(path)
    events = _events()
    assert events[-1]["kind"] == "pairing_unavailable"
    assert not any(event["kind"] == "pairing_resolved" for event in events)
    path.write_text("invalid TOML!")
    with pytest.raises(assignments.NoLivePairing, match="quota"):
        _resolve(path)
    assert _events() == events


def test_transaction_failure_publishes_neither_resolution_nor_assignment(
    work_unit_ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, PREFERRED)

    def crash(*args, **kwargs):
        raise OSError("synthetic evidence read failure")

    monkeypatch.setattr(assignments, "recent_dispatch_probe", crash)
    with pytest.raises(OSError, match="synthetic"):
        _resolve(path)
    assert _events() == []
    monkeypatch.setattr(assignments, "recent_dispatch_probe", lambda *a, **kw: (True, None))
    assert _resolve(path).resolution_id


def test_concurrent_resolvers_publish_one_history(
    work_unit_ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, PREFERRED)
    asked = []

    def probe(harness, model, **kwargs):
        asked.append((harness, model))
        return True, None

    monkeypatch.setattr(assignments, "recent_dispatch_probe", probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _resolve(path), range(2)))
    assert results[0] == results[1]
    assert asked == [(Harness.CODEX, "gpt-5.6-sol"), (Harness.CODEX, "gpt-6-astra")]
    assert len(_events()) == 2


def test_forged_resolution_cannot_become_claimable(
    work_unit_ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, PREFERRED)
    monkeypatch.setattr(assignments, "recent_dispatch_probe", lambda *a, **kw: (True, None))
    actual = _resolve(path)
    for forged in (
        replace(actual, resolution_id="pr_missing"),
        replace(actual, chart_hash="forged"),
    ):
        with pytest.raises(RuntimeError, match="resolution"):
            submit_dispatch_intent("senior", "forged", pairing_assignment=forged.to_payload())
    with tx() as c:
        assert c.execute("SELECT count(*) AS n FROM dispatch_intents").fetchone()["n"] == 0


@pytest.mark.parametrize(
    "fallback",
    [
        'mode="same_provider"',
        'mode="ranked"',
        'mode="explicit", pairings=["cross-vendor", "claude-only"]',
    ],
)
def test_fallback_policy_bounds_and_order(
    work_unit_ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: str
) -> None:
    path = _config(tmp_path, 'mode="preferred"\npairing="codex-only"\nfallback={' + fallback + "}")
    # Sol is spent but Terra is eligible, so same-provider fallback must stay on Codex.
    monkeypatch.setattr(
        assignments,
        "recent_dispatch_probe",
        lambda harness, model, **kw: (model != "gpt-5.6-sol", "quota"),
    )
    result = _resolve(path)
    if "same_provider" in fallback:
        assert result.pairing.senior.harness is result.pairing.staff.harness is Harness.CODEX
    elif "explicit" in fallback:
        assert result.pairing.senior.model == "claude-sonnet-5"
        assert result.pairing.staff.model == "claude-opus-5"
    else:
        assert result.pairing.staff.harness is Harness.CLAUDE
    assert any(event["kind"] == "fallback_selected" for event in _events())


def test_same_provider_exhaustion_never_probes_another_harness(
    work_unit_ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(
        tmp_path, 'mode="preferred"\npairing="codex-only"\nfallback={mode="same_provider"}'
    )
    asked = []

    def probe(harness, model, **kwargs):
        asked.append(harness)
        return harness is not Harness.CODEX, "quota"

    monkeypatch.setattr(assignments, "recent_dispatch_probe", probe)
    with pytest.raises(assignments.NoLivePairing):
        _resolve(path)
    assert set(asked) == {Harness.CODEX}


def test_explicit_fallback_keeps_declared_order_instead_of_ranking(
    work_unit_ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(
        tmp_path,
        'mode="preferred"\npairing="codex-only"\n'
        'fallback={mode="explicit", pairings=["claude-only", "terra"]}',
    )
    path.write_text(
        path.read_text() + '\n[pairings.terra.senior]\nharness="codex"\n'
        'model="gpt-5.6-terra"\nreasoning_effort="max"\n'
        '[pairings.terra.staff]\nharness="claude"\nmodel="claude-opus-5"\nreasoning_effort="max"\n'
    )
    monkeypatch.setattr(
        assignments,
        "recent_dispatch_probe",
        lambda harness, model, **kw: (model != "gpt-5.6-sol", "quota"),
    )
    assert _resolve(path).pairing.senior.model == "claude-sonnet-5"


@pytest.mark.parametrize(
    "policy",
    [
        'mode="preferred"\npairing="codex-only"',
        'mode="preferred"\npairing="codex-only"\nfallback={mode="explicit", pairings=[]}',
        'mode="preferred"\npairing="codex-only"\nfallback={mode="explicit", pairings=["missing"]}',
        'mode="preferred"\npairing="codex-only"\n'
        'fallback={mode="explicit", pairings=["codex-only"]}',
        'mode="preferred"\npairing="codex-only"\n'
        'fallback={mode="explicit", pairings=["claude-only", "claude-only"]}',
        'mode="preferred"\npairing="codex-only"\nfallback={mode="ranked", extra=true}',
        'mode="fixed"\npairing="codex-only"\nfallback={mode="ranked"}',
    ],
)
def test_invalid_fallback_is_not_an_availability_failure(tmp_path: Path, policy: str) -> None:
    with pytest.raises(ValueError, match="pairing|fallback"):
        load_staffing(_config(tmp_path, policy))


def test_queue_retention_cannot_erase_pairing_replay_authority(
    work_unit_ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_first_agent_os.coordination import execution as ledger_execution

    path = _config(tmp_path, PREFERRED)
    monkeypatch.setattr(assignments, "recent_dispatch_probe", lambda *a, **kw: (True, None))
    assignment = _resolve(path)
    submitted = submit_dispatch_intent(
        "senior",
        "historical resolved attempt",
        idempotency_key="retained-resolution",
        pairing_assignment=assignment.to_payload(),
    )
    assignments.invalidate_assignment(
        assignment, harness=Harness.CODEX, model="gpt-5.6-sol", reason="quota"
    )
    with tx() as c:
        c.execute("UPDATE ledger_events SET created_at=1")
        c.execute("UPDATE dispatch_intents SET created_at=1, completed_at=2, status='FAILED'")
    before = _events()
    ledger_execution.gc_ledger(retention_seconds=60)
    assert _events() == before
    assert assignments.assignment_for_idempotency_key("retained-resolution") == assignment
    assert assignments.assignment_for_intent(submitted["intent_id"]) == assignment
    path.write_text("invalid TOML!")
    assert _resolve(path) == assignment
