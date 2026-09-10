# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Public CLI regressions: completion cannot invent a claim or rewrite a fact."""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]


def _command(root: Path, *args: str) -> dict[str, Any]:
    process = subprocess.run(
        [
            sys.executable,
            str(REPO / "agent_coordination_mcp.py"),
            "--root",
            str(root),
            "--no-next-commands",
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.stdout, process.stderr
    payload = json.loads(process.stdout)
    assert isinstance(payload, dict)
    return payload


def _row(root: Path, identity: str) -> dict[str, Any]:
    return next(
        row
        for row in _command(root, "list_dispatch_intents")["intents"]
        if row["intent_id"] == identity
    )


def _submit(root: Path, *, claim: bool) -> str:
    created = _command(root, "submit_dispatch_intent", "junior", "boundary canary")
    assert created["ok"], created
    identity = str(created["intent_id"])
    if claim:
        claimed = _command(root, "claim_next_dispatch_intent", "--claimed-by", "test-worker")
        assert claimed["ok"] and claimed["intent"]["intent_id"] == identity
    return identity


@pytest.mark.parametrize("prior", ["PENDING", "FAILED", "CANCELED"])
def test_invalid_public_completion_preserves_the_entire_record(tmp_path: Path, prior: str) -> None:
    identity = _submit(tmp_path, claim=prior == "FAILED")
    if prior == "FAILED":
        assert _command(
            tmp_path, "complete_dispatch_intent", identity, "FAILED", "--error", "retained failure"
        )["ok"]
    elif prior == "CANCELED":
        assert _command(tmp_path, "cancel_dispatch_intent", identity)["ok"]
    before = _row(tmp_path, identity)
    refused = _command(
        tmp_path, "complete_dispatch_intent", identity, "DONE", "--result", "fabricated replacement"
    )
    assert refused["ok"] is False
    assert refused["error"] == "invalid_completion_transition"
    assert _row(tmp_path, identity) == before


def test_identical_replay_does_not_rewrite_completion_time(tmp_path: Path) -> None:
    identity = _submit(tmp_path, claim=True)
    assert _command(tmp_path, "complete_dispatch_intent", identity, "DONE", "--result", "answer")[
        "ok"
    ]
    before = _row(tmp_path, identity)
    replay = _command(tmp_path, "complete_dispatch_intent", identity, "DONE", "--result", "answer")
    assert replay["ok"] and replay["identical_replay"]
    assert _row(tmp_path, identity) == before


def test_same_terminal_status_cannot_replace_payload(tmp_path: Path) -> None:
    identity = _submit(tmp_path, claim=True)
    assert _command(tmp_path, "complete_dispatch_intent", identity, "DONE", "--result", "first")[
        "ok"
    ]
    before = _row(tmp_path, identity)
    assert not _command(
        tmp_path, "complete_dispatch_intent", identity, "DONE", "--result", "second"
    )["ok"]
    assert _row(tmp_path, identity) == before


def test_competing_terminal_writers_have_one_winner(tmp_path: Path) -> None:
    identity = _submit(tmp_path, claim=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _command, tmp_path, "complete_dispatch_intent", identity, status, "--result", status
            )
            for status in ("DONE", "FAILED")
        ]
        outcomes = [future.result() for future in futures]
    assert sum(outcome["ok"] is True for outcome in outcomes) == 1
    winner = next(outcome for outcome in outcomes if outcome["ok"])
    observed = _row(tmp_path, identity)
    assert observed["status"] == observed["result"] == winner["status"]


def test_unknown_status_is_rejected_before_mutation(tmp_path: Path) -> None:
    identity = _submit(tmp_path, claim=True)
    from local_first_agent_os.coordination.dispatch import complete_dispatch_intent

    before = _row(tmp_path, identity)
    assert complete_dispatch_intent(identity, "SUCCEEDED")["error"] == "invalid_status"
    assert _row(tmp_path, identity) == before


@pytest.mark.parametrize("authority", ["absent", "wrong"])
def test_direct_completion_requires_actual_operator_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authority: str
) -> None:
    from local_first_agent_os.coordination.dispatch import complete_dispatch_intent
    from local_first_agent_os.operator_identity import OperatorIdentityRefused

    identity = _submit(tmp_path, claim=True)
    before = _row(tmp_path, identity)
    with monkeypatch.context() as request_environment:
        if authority == "absent":
            request_environment.delenv("LOCAL_AGENT_OPERATOR_TOKEN")
        elif authority == "wrong":
            request_environment.setenv("LOCAL_AGENT_OPERATOR_TOKEN", "wrong-token")
        with pytest.raises(OperatorIdentityRefused):
            complete_dispatch_intent(identity, "DONE", result="unauthorized claim")
    assert _row(tmp_path, identity) == before


def _claimed_code(root: Path) -> str:
    created = _command(
        root,
        "submit_dispatch_intent",
        "junior",
        "typed completion canary",
        "--kind",
        "code",
        "--target-project-id",
        "local-first-agent-os",
    )
    assert created["ok"], created
    identity = str(created["intent_id"])
    claimed = _command(root, "claim_next_dispatch_intent", "--claimed-by", "host-supervisor")
    assert claimed["intent"]["intent_id"] == identity
    return identity


def _report(identity: str, status: str = "COMPLETED") -> str:
    return json.dumps(
        {
            "schema_version": "dispatch_runner_result.v1",
            "intent_id": identity,
            "target_project_id": "local-first-agent-os",
            "run_result": {"status": status, "target_project_id": "local-first-agent-os"},
        }
    )


@pytest.mark.parametrize("corruption", ["malformed", "unknown_schema", "wrong_subject", "failed"])
def test_invalid_code_completion_is_retained_as_diagnostic_not_success(
    tmp_path: Path, corruption: str
) -> None:
    identity = _claimed_code(tmp_path)
    payload = _report(identity)
    if corruption == "malformed":
        payload = "{ invalid JSON containing a secret-that-must-not-be-logged"
    elif corruption == "unknown_schema":
        payload = payload.replace("dispatch_runner_result.v1", "dispatch_runner_result.v999")
    elif corruption == "wrong_subject":
        payload = _report("different-intent")
    elif corruption == "failed":
        payload = _report(identity, "FAILED")
    before = _row(tmp_path, identity)
    refused = _command(tmp_path, "complete_dispatch_intent", identity, "DONE", "--result", payload)
    assert not refused["ok"], refused
    assert refused["error"] == "dispatch_report_contract_violation"
    assert _row(tmp_path, identity) == before
    replay = _command(tmp_path, "complete_dispatch_intent", identity, "DONE", "--result", payload)
    assert replay["diagnostic_event_id"] == refused["diagnostic_event_id"]
    events = _command(tmp_path, "list_ledger_events")["events"]
    diagnostic = next(
        event for event in events if event["event_id"] == refused["diagnostic_event_id"]
    )
    assert diagnostic["event_type"] == "dispatch_contract_violation"
    assert diagnostic["payload"]["intent_id"] == identity
    assert "secret-that-must-not-be-logged" not in json.dumps(diagnostic)
    assert len([event for event in events if event["event_id"] == diagnostic["event_id"]]) == 1


@pytest.mark.parametrize(
    ("run_status", "dispatch_status"),
    [
        ("COMPLETED", "DONE"),
        ("FAILED", "FAILED"),
        ("TIMED_OUT", "FAILED"),
        ("CANCELED", "FAILED"),
        ("UNAVAILABLE", "FAILED"),
    ],
)
def test_declared_completion_outcomes_remain_usable(
    tmp_path: Path, run_status: str, dispatch_status: str
) -> None:
    identity = _claimed_code(tmp_path)
    completed = _command(
        tmp_path,
        "complete_dispatch_intent",
        identity,
        dispatch_status,
        "--result",
        _report(identity, run_status),
    )
    assert completed["ok"], completed
    assert _row(tmp_path, identity)["status"] == dispatch_status


@pytest.mark.parametrize("corruption", ["project_claim", "success_claim", "agent_origin"])
def test_projectless_runner_failure_cannot_claim_execution_authority(
    tmp_path: Path, corruption: str
) -> None:
    created = _command(
        tmp_path, "submit_dispatch_intent", "senior", "missing project canary", "--kind", "code"
    )
    assert created["ok"]
    identity = str(created["intent_id"])
    claimed = _command(tmp_path, "claim_next_dispatch_intent", "--claimed-by", "host-supervisor")
    assert claimed["intent"]["intent_id"] == identity
    payload = {
        "schema_version": "dispatch_runner_result.v1",
        "intent_id": identity,
        "result_origin": "runner_crash",
        "result_state": "FAILED",
        "promotion_state": "RESULT_RECORDED",
        "run_result": {"status": "FAILED"},
    }
    requested = "FAILED"
    if corruption == "project_claim":
        payload["target_project_id"] = "unrelated-project"
    elif corruption == "success_claim":
        payload["result_state"] = "COMPLETED"
        payload["run_result"] = {"status": "COMPLETED"}
        requested = "DONE"
    else:
        payload["result_origin"] = "AUTOMATED"
    before = _row(tmp_path, identity)
    refused = _command(
        tmp_path, "complete_dispatch_intent", identity, requested, "--result", json.dumps(payload)
    )
    assert not refused["ok"]
    assert refused["error"] == "dispatch_report_contract_violation"
    assert _row(tmp_path, identity) == before
