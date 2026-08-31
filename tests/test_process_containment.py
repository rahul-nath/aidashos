# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

from local_first_agent_os.process_containment import contained_frontier_process
from local_first_agent_os.spawn_authority import ReadOnlyInspection, UnattendedImplementation
from local_first_agent_os.staffing import FrontierHarness

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the production containment adapter is macOS Seatbelt",
)


def _run(
    command: tuple[str, ...], cwd: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_frontier_environment_carries_context_but_no_control_plane_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LOCAL_AGENT_COORDINATION_DATABASE_URL", "postgresql://writer")
    monkeypatch.setenv("LOCAL_AGENT_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    context = {"LOCAL_AGENT_CONTEXT_JSON": '{"work_unit_id":"wu-1"}'}

    with contained_frontier_process(
        ("/usr/bin/true",),
        tmp_path,
        posture=ReadOnlyInspection(),
        harness=FrontierHarness.CODEX,
        overrides=context,
    ) as contained:
        environment = dict(contained.environment)

    assert environment["LOCAL_AGENT_CONTEXT_JSON"] == context["LOCAL_AGENT_CONTEXT_JSON"]
    assert "LOCAL_AGENT_COORDINATION_DATABASE_URL" not in environment
    assert "LOCAL_AGENT_OPERATOR_TOKEN" not in environment
    assert "LOCAL_AGENT_OPERATOR_TOKEN_FILE" not in environment
    assert "OPENAI_API_KEY" not in environment


def test_implementation_can_write_only_its_leased_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    outside = tmp_path / "outside"
    worktree.mkdir()
    outside.mkdir()
    inside_file = worktree / "inside.txt"
    outside_file = outside / "outside.txt"

    with contained_frontier_process(
        ("/usr/bin/touch", str(inside_file)),
        worktree,
        posture=UnattendedImplementation(),
        harness=FrontierHarness.CODEX,
    ) as contained:
        allowed = _run(contained.command, worktree, dict(contained.environment))
    with contained_frontier_process(
        ("/usr/bin/touch", str(outside_file)),
        worktree,
        posture=UnattendedImplementation(),
        harness=FrontierHarness.CODEX,
    ) as contained:
        refused = _run(contained.command, worktree, dict(contained.environment))

    assert allowed.returncode == 0
    assert inside_file.is_file()
    assert refused.returncode != 0
    assert not outside_file.exists()


def test_read_only_process_cannot_write_its_checkout(tmp_path: Path) -> None:
    attempted = tmp_path / "review-write.txt"

    with contained_frontier_process(
        ("/usr/bin/touch", str(attempted)),
        tmp_path,
        posture=ReadOnlyInspection(),
        harness=FrontierHarness.CLAUDE,
    ) as contained:
        result = _run(contained.command, tmp_path, dict(contained.environment))

    assert result.returncode != 0
    assert not attempted.exists()


def test_agent_process_cannot_read_the_operator_token(tmp_path: Path) -> None:
    from local_first_agent_os.operator_identity import operator_token_file

    token_file = operator_token_file()
    assert token_file.is_file()

    with contained_frontier_process(
        ("/bin/cat", str(token_file)),
        tmp_path,
        posture=ReadOnlyInspection(),
        harness=FrontierHarness.CODEX,
    ) as contained:
        result = _run(contained.command, tmp_path, dict(contained.environment))

    assert result.returncode != 0
    assert "test-operator-token" not in result.stdout


def test_agent_process_cannot_read_an_undeclared_host_file(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    outside = tmp_path / "host-secret.txt"
    worktree.mkdir()
    outside.write_text("host secret\n", encoding="utf-8")

    with contained_frontier_process(
        ("/bin/cat", str(outside)),
        worktree,
        posture=UnattendedImplementation(),
        harness=FrontierHarness.CODEX,
    ) as contained:
        result = _run(contained.command, worktree, dict(contained.environment))

    assert result.returncode != 0
    assert "host secret" not in result.stdout


def test_only_the_reader_database_endpoint_enters_the_network_boundary(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with socket.socket() as reader, socket.socket() as writer:
        reader.bind(("127.0.0.1", 0))
        writer.bind(("127.0.0.1", 0))
        reader.listen()
        writer.listen()
        reader_port = reader.getsockname()[1]
        writer_port = writer.getsockname()[1]
        reader_url = f"postgresql://ledger_reader@127.0.0.1:{reader_port}/ledger"
        overrides = {"LOCAL_AGENT_LEDGER_READER_DATABASE_URL": reader_url}

        def connect(port: int) -> tuple[str, ...]:
            return (
                sys.executable,
                "-c",
                f"import socket; socket.create_connection(('127.0.0.1', {port}), 1).close()",
            )

        with contained_frontier_process(
            connect(reader_port),
            worktree,
            posture=ReadOnlyInspection(),
            harness=FrontierHarness.CODEX,
            overrides=overrides,
        ) as contained:
            environment = dict(contained.environment)
            reader_result = _run(contained.command, worktree, environment)
        with contained_frontier_process(
            connect(writer_port),
            worktree,
            posture=ReadOnlyInspection(),
            harness=FrontierHarness.CODEX,
            overrides=overrides,
        ) as contained:
            writer_result = _run(contained.command, worktree, dict(contained.environment))

    assert environment["AGENT_COORDINATION_DATABASE_URL"] == reader_url
    assert environment["LOCAL_AGENT_COORDINATION_DATABASE_URL"] == reader_url
    assert reader_result.returncode == 0
    assert writer_result.returncode != 0
