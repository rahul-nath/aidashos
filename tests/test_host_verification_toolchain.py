# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The real offline gate sees exact Git identity and only its installed tools."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import test_host_verification_receipts
from test_host_verification_receipts import GateFixture

from local_first_agent_os import host_verification as host
from local_first_agent_os.constants import PROCESS_TIMEOUT_EXIT_CODE
from local_first_agent_os.coordination.execution import complete_execution_lease
from local_first_agent_os.coordination.store import tx

gate_fixture = test_host_verification_receipts.gate_fixture


def _commit(repository: Path) -> str:
    for arguments in (
        ("add", "."),
        ("-c", "user.name=Fixture", "-c", "user.email=fixture@invalid", "commit", "-qm", "gate"),
    ):
        subprocess.run(("git", "-C", str(repository), *arguments), check=True, capture_output=True)
    return subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip()


def test_frozen_snapshot_contains_only_its_exact_local_git_objects(
    gate_fixture: GateFixture, tmp_path: Path
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    config = gate_fixture.repository / ".git" / "config"
    config.write_text(config.read_text() + '\n[remote "private"]\n\turl = private-canary\n')
    source = host._snapshot(
        gate_fixture.repository, gate_fixture.commit, gate_fixture.commit, snapshot
    )
    assert host._git(snapshot, "rev-parse", "HEAD").decode().strip() == source.commit
    assert host._git(snapshot, "rev-parse", "HEAD^{tree}").decode().strip() == source.tree
    assert host._git(snapshot, "cat-file", "commit", "HEAD") == host._git(
        gate_fixture.repository, "cat-file", "commit", gate_fixture.commit
    )
    assert host._git(snapshot, "diff", "--exit-code", "HEAD") == b""
    assert not (snapshot / ".git" / "objects" / "info" / "alternates").exists()
    assert not (snapshot / ".git" / "hooks").exists()
    assert "private-canary" not in (snapshot / ".git" / "config").read_text()


def test_real_frozen_source_runs_git_uv_pytest_ruff_pyright_and_node(
    gate_fixture: GateFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = gate_fixture.repository
    current = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PATH", str(repository.parent) + os.pathsep + os.environ["PATH"])
    package = repository / "src" / "local_first_agent_os"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "runtime_source.py").write_bytes(
        (current / "src" / "local_first_agent_os" / "runtime_source.py").read_bytes()
    )
    (package / "constants.py").write_bytes(
        (current / "src" / "local_first_agent_os" / "constants.py").read_bytes()
    )
    (repository / "test_runtime_source.py").write_bytes(
        (current / "tests" / "test_runtime_source.py").read_bytes()
    )
    (repository / "pyproject.toml").write_text(
        '[project]\nname="frozen-verification-fixture"\nversion="0.1.0"\n'
        'requires-python=">=3.13"\n[tool.pytest.ini_options]\npythonpath=["src"]\n'
        '[tool.pyright]\npythonVersion="3.13"\nextraPaths=["src"]\n'
    )
    (repository / "verify_identity.py").write_text(
        "import json\nimport subprocess\nfrom pathlib import Path\n"
        "from local_first_agent_os.runtime_source import runtime_checkout, runtime_revision\n"
        "head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
        "assert runtime_checkout() == Path.cwd()\n"
        "assert runtime_revision() == head\n"
        "print(json.dumps({'commit': head, 'checkout': str(runtime_checkout())}))\n"
    )
    with (repository / "gate.py").open("a") as gate:
        gate.write(
            "for action in [lambda: Path('.git/HEAD').write_text('tamper'), "
            f"lambda: Path({str(current / 'pyproject.toml')!r}).read_text()]:\n"
            "    try: action()\n"
            "    except PermissionError: pass\n"
            "    else: raise AssertionError('metadata/source containment was bypassed')\n"
        )
    commit = _commit(repository)
    center = host.load_project_center()
    project = replace(
        center.projects[0],
        verification_commands=[
            "uv run --offline --no-sync python verify_identity.py",
            "uv run --offline --no-sync pytest -q test_runtime_source.py",
            "uv run --offline --no-sync ruff check src test_runtime_source.py",
            "uv run --offline --no-sync pyright src",
            "node --version",
            "uv run --offline --no-sync python gate.py",
        ],
    )
    monkeypatch.setattr(host, "load_project_center", lambda: replace(center, projects=(project,)))
    outcome = replace(gate_fixture, commit=commit).run()
    assert isinstance(outcome, host.VerificationPassed), (
        "\n".join(
            f"{capture.command}: {capture.stdout} {capture.stderr}"
            for capture in outcome.captures
            if capture.exit_code != 0
        )
        if isinstance(outcome, host.VerificationFailed)
        else outcome
    )
    assert len(outcome.captures) == 6
    assert all(capture.exit_code == 0 for capture in outcome.captures)
    assert json.loads(outcome.captures[0].stdout)["commit"] == commit
    assert "8 passed" in outcome.captures[1].stdout
    assert "0 errors" in outcome.captures[3].stdout
    assert "source write and outside read denied" in outcome.captures[5].stdout


def test_git_replacement_refs_cannot_change_the_named_snapshot(
    gate_fixture: GateFixture, tmp_path: Path
) -> None:
    repository = gate_fixture.repository
    (repository / "value.txt").write_text("replacement tree must not be certified")
    replacement = _commit(repository)
    subprocess.run(
        ("git", "-C", str(repository), "replace", gate_fixture.commit, replacement),
        capture_output=True,
        check=True,
    )
    snapshot = tmp_path / "unreplaced-source"
    snapshot.mkdir()
    source = host._snapshot(repository, gate_fixture.commit, gate_fixture.commit, snapshot)
    assert source.commit == gate_fixture.commit
    assert (snapshot / "value.txt").read_text() == "sealed source"
    assert not (snapshot / ".git" / "refs" / "replace").exists()
    assert host._git(snapshot, "rev-parse", "HEAD").decode().strip() == gate_fixture.commit


def test_snapshot_git_metadata_preserves_sha256_object_identity(tmp_path: Path) -> None:
    repository = tmp_path / "sha256-repository"
    repository.mkdir()
    subprocess.run(
        ("git", "-C", str(repository), "init", "--object-format=sha256", "-q"),
        capture_output=True,
        check=True,
    )
    (repository / "content.txt").write_text("exact SHA256 Git object")
    commit = _commit(repository)
    assert len(commit) == 64
    snapshot = tmp_path / "sha256-snapshot"
    snapshot.mkdir()
    source = host._snapshot(repository, commit, commit, snapshot)
    assert source.commit == commit
    assert host._git(snapshot, "rev-parse", "HEAD^{tree}").decode().strip() == source.tree
    assert host._git(snapshot, "show", "HEAD:content.txt") == b"exact SHA256 Git object"


def test_snapshot_preserves_repeated_blob_and_subtree_objects(tmp_path: Path) -> None:
    repository = tmp_path / "repeated-objects"
    repository.mkdir()
    subprocess.run(("git", "-C", str(repository), "init", "-q"), check=True, capture_output=True)
    for directory in ("left", "right"):
        (repository / directory).mkdir()
        for name in ("first.txt", "second.txt"):
            (repository / directory / name).write_bytes(b"shared Git object\n")
    commit = _commit(repository)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    source = host._snapshot(repository, commit, commit, snapshot)
    assert host._git(repository, "rev-parse", "HEAD:left") == host._git(
        repository, "rev-parse", "HEAD:right"
    )
    assert host._git(snapshot, "diff", "--exit-code", "HEAD") == b""
    assert host._git(snapshot, "rev-parse", "HEAD").decode().strip() == source.commit
    for directory in ("left", "right"):
        for name in ("first.txt", "second.txt"):
            path = snapshot / directory / name
            assert path.read_bytes() == b"shared Git object\n"
            assert path.stat().st_mode & 0o222 == 0
    assert len(tuple((snapshot / ".git" / "objects").glob("*/*"))) == 4


@pytest.mark.parametrize("existing", ["matching", "corrupt", "symlink"])
def test_git_object_installation_never_rewrites_existing_identity(
    tmp_path: Path, existing: str
) -> None:
    content = b"immutable object"
    framed = b"blob 16\0" + content
    identity = hashlib.sha1(framed).hexdigest()
    path = tmp_path / ".git" / "objects" / identity[:2] / identity[2:]
    canary = tmp_path / "outside-object-store"
    canary.write_bytes(b"outside must remain unchanged")
    if existing == "matching":
        host._store_git_object(tmp_path, identity, "blob", content)
        before = path.stat()
        retained = path.read_bytes()
        host._store_git_object(tmp_path, identity, "blob", content)
        assert path.read_bytes() == retained
        assert path.stat().st_ino == before.st_ino
        assert path.stat().st_mtime_ns == before.st_mtime_ns
        assert path.stat().st_mode & 0o222 == 0
    else:
        path.parent.mkdir(parents=True)
        if existing == "corrupt":
            path.write_bytes(b"corrupt Git object")
            path.chmod(0o444)
        else:
            path.symlink_to(canary)
        with pytest.raises(ValueError, match="existing verification Git object"):
            host._store_git_object(tmp_path, identity, "blob", content)
        assert canary.read_bytes() == b"outside must remain unchanged"
        if existing == "corrupt":
            assert path.read_bytes() == b"corrupt Git object"
        else:
            assert path.is_symlink()


def test_gate_deadline_retains_only_started_processes(
    gate_fixture: GateFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = gate_fixture.repository
    (repository / "first.py").write_text("import time\ntime.sleep(0.5)\nprint('first completed')\n")
    (repository / "slow.py").write_text(
        "import time\nprint('slow started', flush=True)\ntime.sleep(10)\n"
    )
    (repository / "never.py").write_text("raise AssertionError('deadline must prevent launch')\n")
    commit = _commit(repository)
    center = host.load_project_center()
    project = replace(
        center.projects[0],
        verification_commands=[
            f"{sys.executable} -I {name}.py" for name in ("first", "slow", "never")
        ],
    )
    monkeypatch.setattr(host, "load_project_center", lambda: replace(center, projects=(project,)))
    outcome = host.run_registered_verification(
        intent_id=gate_fixture.intent_id,
        lease_id=gate_fixture.lease_id,
        worker_id="host-gate-worker",
        source_repository=repository,
        source_commit=commit,
        base_commit=commit,
        timeout_seconds=3,
    )
    assert isinstance(outcome, host.VerificationFailed), outcome
    assert len(outcome.captures) == 2
    assert outcome.captures[0].exit_code == 0
    assert outcome.captures[1].exit_code == PROCESS_TIMEOUT_EXIT_CODE
    assert "slow started" in outcome.captures[1].stdout
    with tx() as connection:
        row = connection.execute(
            "SELECT payload_json, output_json FROM host_verification_receipts WHERE receipt_id=?",
            (outcome.receipt_id,),
        ).fetchone()
    assert row is not None
    receipt = host.Receipt.model_validate_json(row["payload_json"])
    assert len(receipt.commands) == 3
    assert receipt.execution_end.kind == "deadline_exceeded"
    assert receipt.execution_end.started_command_count == 2
    assert len(json.loads(row["output_json"])) == 2


@pytest.mark.parametrize(
    ("command", "outcome_class", "lease_status"),
    [
        ("printf passed", host.VerificationPassed, "COMPLETED"),
        ("printf failed; exit 1", host.VerificationFailed, "FAILED"),
        ("kill -TERM $$", host.VerificationCancelled, "CANCELED"),
    ],
)
def test_retained_outcome_replays_its_exact_processes_without_qualifying_failure(
    gate_fixture: GateFixture,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    outcome_class: type[
        host.VerificationPassed | host.VerificationFailed | host.VerificationCancelled
    ],
    lease_status: str,
) -> None:
    center = host.load_project_center()
    project = replace(center.projects[0], verification_commands=[command])
    monkeypatch.setattr(host, "load_project_center", lambda: replace(center, projects=(project,)))
    original = gate_fixture.run()
    assert isinstance(original, outcome_class), original
    complete_execution_lease(gate_fixture.lease_id, lease_status)
    subject = host.verification_subject_for_dispatch(gate_fixture.intent_id)
    assert subject is not None
    replay = host.resolve_retained_verification_outcome(
        original.receipt_id, subject, gate_fixture.lease_id, gate_fixture.commit
    )
    assert isinstance(replay, outcome_class), replay
    assert replay == original
    for lease_id, commit in (
        ("another-lease", gate_fixture.commit),
        (gate_fixture.lease_id, "a" * 40),
    ):
        rejected = host.resolve_retained_verification_outcome(
            original.receipt_id, subject, lease_id, commit
        )
        assert isinstance(rejected, host.VerificationUnavailable)
    evidence = host.resolve_verification_receipt(original.receipt_id, subject)
    if outcome_class is host.VerificationPassed:
        assert isinstance(evidence, host.VerifiedReceiptReference)
    else:
        assert isinstance(evidence, host.RetainedLegacyObservation)
