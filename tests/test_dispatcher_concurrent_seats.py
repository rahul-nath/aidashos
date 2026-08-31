# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The dispatcher's per-tier seats: overlap where staffing allows it, serial
where it does not, and a failed pipeline hands its seat back.

The defect these tests pin (observed 2026-08-10): milestones 1 and 2 of one
WorkUnit submitted their dispatch intents in the same second and ran strictly
serially, because `dispatch_pending_intents` ran each claimed pipeline to its
terminal status before claiming again. Every PENDING intent is runnable by
construction - milestones submit intents only once their dependencies are
satisfied - so the fix is seats, not DAG knowledge: the loop claims up to each
tier's free seats and runs the claimed pipelines on a bounded worker pool.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from local_first_agent_os.contracts import SourceType, WorkflowStatus, WorkspaceId
from local_first_agent_os.coordination import (
    ClaimNextDispatchIntent,
    CompleteDispatchIntent,
    DispatchTerminalStatus,
)
from local_first_agent_os.coordination.availability import LedgerUnavailable
from local_first_agent_os.dispatcher import UNAVAILABLE_INTERVAL_SECONDS, LedgerDispatcher
from local_first_agent_os.dispatcher_runner import DispatcherIntentRunner
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.pow_wow import run_coordination_command
from local_first_agent_os.settings import Settings
from local_first_agent_os.staffing import dispatch_seat_counts, load_bench
from local_first_agent_os.workflow import WorkflowEngine


def _coord(root: Path, args: list[str]) -> dict:
    return run_coordination_command(args, root=root)


def _submit_two_senior_siblings(root: Path) -> tuple[str, str]:
    first = _coord(root, ["submit_dispatch_intent", "senior", "first sibling milestone"])
    second = _coord(root, ["submit_dispatch_intent", "senior", "second sibling milestone"])
    return first["intent_id"], second["intent_id"]


def test_two_same_tier_intents_with_two_seats_run_overlapped(tmp_path: Path) -> None:
    """With two senior seats, sibling intents claimed together run at once.

    The barrier is the proof: each pipeline blocks until the other has started,
    so both runners being inside the barrier at the same time is exactly "both
    pipelines start before either finishes". A serial loop breaks the barrier
    by timeout and the intents surface as FAILED rather than hanging the test.
    """

    root = tmp_path / "coord"
    first_id, second_id = _submit_two_senior_siblings(root)
    both_started = threading.Barrier(2)

    def runner(intent):
        try:
            both_started.wait(timeout=15.0)
        except threading.BrokenBarrierError:
            return (
                DispatchTerminalStatus.FAILED,
                None,
                "the sibling pipeline never started while this one ran",
            )
        return (DispatchTerminalStatus.DONE, f"ran {intent['prompt']}", None)

    dispatcher = LedgerDispatcher(
        runner,
        name="overlap-dispatcher",
        settings=Settings(coordination_root=root),
        seats={"senior": 2},
    )

    dispatched = dispatcher.dispatch_pending_intents(interval_seconds=0.05, max_polls=1)

    # One poll is one sweep over the claim lanes: both free seats were filled
    # before the loop drained, so a single poll dispatched both siblings.
    assert dispatched == 2
    assert {outcome.status for outcome in dispatcher.last_outcomes} == {"DONE"}
    done = _coord(root, ["list_dispatch_intents", "--status", "DONE"])["intents"]
    assert {row["intent_id"] for row in done} == {first_id, second_id}


def test_one_seat_preserves_the_serial_loop(tmp_path: Path) -> None:
    """seats=1 is today's behavior: FIFO claims, one pipeline at a time."""

    root = tmp_path / "coord"
    first_id, second_id = _submit_two_senior_siblings(root)
    windows: list[tuple[str, float, float]] = []
    windows_lock = threading.Lock()

    def runner(intent):
        started = time.monotonic()
        time.sleep(0.05)
        finished = time.monotonic()
        with windows_lock:
            windows.append((intent["intent_id"], started, finished))
        return (DispatchTerminalStatus.DONE, None, None)

    dispatcher = LedgerDispatcher(
        runner,
        name="serial-dispatcher",
        settings=Settings(coordination_root=root),
        seats={"senior": 1},
    )

    dispatched = dispatcher.dispatch_pending_intents(interval_seconds=0.0, max_polls=3)

    assert dispatched == 2
    assert [entry[0] for entry in windows] == [first_id, second_id]
    first_window, second_window = windows
    assert first_window[2] <= second_window[1], (
        "with one seat the second pipeline must not start before the first finishes"
    )


def test_a_failed_pipeline_hands_its_seat_back(tmp_path: Path) -> None:
    """A pipeline crash fails its intent and frees the seat for the next claim."""

    root = tmp_path / "coord"
    first_id, second_id = _submit_two_senior_siblings(root)

    def runner(intent):
        if intent["intent_id"] == first_id:
            raise RuntimeError("pipeline died")
        return (DispatchTerminalStatus.DONE, None, None)

    dispatcher = LedgerDispatcher(
        runner,
        name="release-dispatcher",
        settings=Settings(coordination_root=root),
        seats={"senior": 1},
    )

    dispatched = dispatcher.dispatch_pending_intents(interval_seconds=0.0, max_polls=3)

    assert dispatched == 2
    statuses = {outcome.intent_id: outcome.status for outcome in dispatcher.last_outcomes}
    assert statuses == {first_id: "FAILED", second_id: "DONE"}
    failed = _coord(root, ["list_dispatch_intents", "--status", "FAILED"])["intents"]
    assert failed[0]["intent_id"] == first_id
    assert "pipeline died" in failed[0]["error"]
    done = _coord(root, ["list_dispatch_intents", "--status", "DONE"])["intents"]
    assert done[0]["intent_id"] == second_id


def test_explicit_seat_map_cannot_invent_a_seat_for_an_unstaffed_scoped_tier() -> None:
    dispatcher = LedgerDispatcher(
        lambda _intent: (DispatchTerminalStatus.DONE, None, None),
        name="unstaffed-dispatcher",
        tier="staff",
        seats={"senior": 2},
    )

    with pytest.raises(ValueError, match="has no seats"):
        dispatcher.dispatch_pending_intents(interval_seconds=0.0, max_polls=1)


def test_an_outage_after_a_partial_sweep_waits_before_retrying() -> None:
    intent = {
        "intent_id": "first-claim",
        "tier": "senior",
        "prompt": "first sibling",
    }
    claim_calls = 0
    waits: list[float] = []

    def coordination(command):
        nonlocal claim_calls
        if isinstance(command, ClaimNextDispatchIntent):
            claim_calls += 1
            if claim_calls == 1:
                return {"ok": True, "intent": intent}
            raise LedgerUnavailable("coordination database is unavailable")
        assert isinstance(command, CompleteDispatchIntent)
        return {"ok": True}

    dispatcher = LedgerDispatcher(
        lambda _intent: (DispatchTerminalStatus.DONE, None, None),
        name="outage-dispatcher",
        seats={"senior": 2},
    )
    dispatcher._coord = coordination  # type: ignore[method-assign]
    dispatcher._idle_wait = (  # type: ignore[method-assign]
        lambda _in_flight, seconds: waits.append(seconds)
    )

    dispatched = dispatcher.dispatch_pending_intents(interval_seconds=0.0, max_polls=1)

    assert dispatched == 1
    assert claim_calls == 2
    assert waits == [UNAVAILABLE_INTERVAL_SECONDS]


# --- the end-to-end proof, with fake frontier CLIs ----------------------------


def _run_git_command(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _run_git_command(["git", "init"], path)
    _run_git_command(["git", "config", "user.email", "test@example.com"], path)
    _run_git_command(["git", "config", "user.name", "Test User"], path)
    (path / "README.md").write_text("# target\n", encoding="utf-8")
    _run_git_command(["git", "add", "README.md"], path)
    _run_git_command(["git", "commit", "-m", "initial"], path)


def _write_linked_projects(config_dir: Path, target_path: Path) -> None:
    (config_dir / "linked_projects.toml").write_text(
        f"""
[center]
id = "local_first_agent_os"
description = "test center"
control_plane_project = "target"
default_saga_project = "target"
default_memory_project = "target"

[[projects]]
id = "target"
kind = "test_repo"
path = {json.dumps(str(target_path))}
status = "active"
read_only = false
description = "test target"
primary_interfaces = ["pytest"]
owns = ["tests"]
avoid = []
verification_commands = ["test -f NEXT_STEP.md"]
""".strip()
        + "\n",
        encoding="utf-8",
    )


_STAFFING_TWO_SENIOR_SEATS = """
seated_pairing = "two-vendor"

[pairings.two-vendor.staff]
harness = "codex"
model = "gpt-5.6-sol"
reasoning_effort = "high"
capacity = 1

[pairings.two-vendor.senior]
harness = "claude"
capacity = 2

[bench.junior]
harness = "pi"
model = "gemma4"
capacity = 4
""".strip()

# The rendezvous is the overlap proof. A pipeline-one invocation records
# overlap only when it observes pipeline two already started while it is itself
# still running, and vice versa. Run serially, pipeline one finishes all of its
# invocations before pipeline two exists, so `overlap-seen-by-one` can never
# appear; only both files together witness concurrency.
_FAKE_CLAUDE_RENDEZVOUS = """#!/usr/bin/env python3
import json
import sys
import time
from pathlib import Path

prompt = sys.argv[-1]
if 'Planning visibility contract: senior_independent_reading.' in prompt:
    print(json.dumps({'type': 'result', 'result': 'independent raw-contract reading'}))
    raise SystemExit(0)
rendezvous = Path('@RENDEZVOUS@')
mine = 'one' if '@TOKEN_ONE@' in prompt else ('two' if '@TOKEN_TWO@' in prompt else None)
if mine is not None:
    peer = 'two' if mine == 'one' else 'one'
    (rendezvous / ('started-' + mine)).write_text('', encoding='utf-8')
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if (rendezvous / ('started-' + peer)).exists():
            (rendezvous / ('overlap-seen-by-' + mine)).write_text('', encoding='utf-8')
            break
        time.sleep(0.05)
Path('NEXT_STEP.md').write_text('proof from fake claude\\n', encoding='utf-8')
print(json.dumps({'type': 'result', 'result': 'created NEXT_STEP.md'}))
"""

_FAKE_CODEX_REVIEWER = """#!/usr/bin/env python3
import sys
from pathlib import Path

if sys.argv[1:3] == ['login', 'status']:
    raise SystemExit(0)
prompt = sys.argv[-1]
if 'Planning visibility contract: staff_independent_reading.' in prompt:
    print('Independent staff raw-contract reading')
    raise SystemExit(0)
if not Path('NEXT_STEP.md').exists():
    print('VERDICT: BLOCK - missing NEXT_STEP.md')
    raise SystemExit(2)
print('VERDICT: APPROVE - NEXT_STEP.md present for the sibling overlap proof')
"""


def _enqueue_approved_gawd_intent(runtime, goal: str) -> str:
    """One approved GAWD milestone, enqueued: returns its dispatch intent id."""

    saga = run_coordination_command(["create_saga", goal], settings=runtime.settings)
    doc = run_coordination_command(
        [
            "create_gawd_doc",
            goal,
            "--saga-id",
            saga["saga_id"],
            "--constraints",
            "Only touch the disposable proof target.",
            "--success-criteria",
            "NEXT_STEP.md is created in the isolated worktree.",
            "--acceptance-criteria",
            "Dispatcher claims and completes the intent.",
            "--task-graph-json",
            '{"schema_version":"new_project_task_graph.v1"}',
        ],
        settings=runtime.settings,
    )
    directive = f"/start /approved-gawd {doc['gawd_doc_id']} --target-project target"
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": directive},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.status == WorkflowStatus.COMPLETED
    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert payload["status"] == "approved_and_enqueued"
    return str(payload["dispatch_intent_id"])


def _legacy_two_approved_gawd_milestones_run_overlapped_on_two_senior_seats(
    tmp_path: Path,
    runtime,
) -> None:
    """End to end: two sibling milestones, two senior seats, one dispatcher.

    The seats come from `configs/staffing.toml` through `load_bench` and
    `dispatch_seat_counts`, exactly as the resident loop reads them. Each
    intent runs its full pow-wow pipeline - independent readings, implementer,
    staff review - against fake frontier CLIs, and the implementers rendezvous
    through the filesystem to witness that both pipelines were alive at once.
    Distinct sagas also exercise the shared bottlenecks the loop must not
    trip over: per-pipeline worktrees and one coordination schema.
    """

    root = tmp_path / "coordination-root"
    target = tmp_path / "target"
    rendezvous = tmp_path / "rendezvous"
    rendezvous.mkdir()
    runtime.settings.coordination_root = root
    runtime.settings.saga_worktree_root = tmp_path / "worktrees"
    _init_git_repo(target)
    _write_linked_projects(runtime.settings.config_dir, target)
    (runtime.settings.config_dir / "staffing.toml").write_text(
        _STAFFING_TWO_SENIOR_SEATS + "\n", encoding="utf-8"
    )
    token_one = "sibling milestone one of the overlap proof"
    token_two = "sibling milestone two of the overlap proof"
    claude = tmp_path / "fake_claude.py"
    claude.write_text(
        _FAKE_CLAUDE_RENDEZVOUS.replace("@RENDEZVOUS@", str(rendezvous))
        .replace("@TOKEN_ONE@", token_one)
        .replace("@TOKEN_TWO@", token_two),
        encoding="utf-8",
    )
    claude.chmod(0o755)
    codex = tmp_path / "fake_codex.py"
    codex.write_text(_FAKE_CODEX_REVIEWER, encoding="utf-8")
    codex.chmod(0o755)

    first_intent = _enqueue_approved_gawd_intent(runtime, token_one)
    second_intent = _enqueue_approved_gawd_intent(runtime, token_two)

    delegate_calls: list[dict] = []
    delegate_lock = threading.Lock()

    def fake_delegate(**kwargs):
        with delegate_lock:
            delegate_calls.append(kwargs)
        return {"ok": True, "output": f"junior context for {kwargs['task_name']}", "metadata": {}}

    runner = DispatcherIntentRunner(
        runtime,
        delegate_fn=fake_delegate,
        claude_bin=str(claude),
        codex_bin=str(codex),
    )
    bench = load_bench(runtime.settings.config_dir / "staffing.toml")
    dispatcher = LedgerDispatcher(
        runner,
        name="sibling-dispatcher",
        settings=runtime.settings,
        seats=dispatch_seat_counts(bench),
    )

    dispatched = dispatcher.dispatch_pending_intents(interval_seconds=0.1, max_polls=1)

    assert dispatched == 2
    statuses = {outcome.intent_id: outcome.status for outcome in dispatcher.last_outcomes}
    assert statuses == {first_intent: "DONE", second_intent: "DONE"}
    assert (rendezvous / "overlap-seen-by-one").exists(), (
        "pipeline one never saw pipeline two while it was still running: "
        "the dispatcher serialized the sibling milestones"
    )
    assert (rendezvous / "overlap-seen-by-two").exists()
    done = _coord(root, ["list_dispatch_intents", "--status", "DONE"])["intents"]
    assert {row["intent_id"] for row in done} == {first_intent, second_intent}
