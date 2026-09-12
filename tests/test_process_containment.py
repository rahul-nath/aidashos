# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from host_test_scope import require_uncontained_scope

from local_first_agent_os.process_containment import (
    _command_read_paths,
    contained_frontier_process,
)
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


@pytest.mark.parametrize("via_env", (False, True), ids=("direct", "env-shebang"))
def test_symlinked_interpreter_exposes_only_its_runtime_library_subtree(
    tmp_path: Path, via_env: bool
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_bin = runtime_root / "bin"
    runtime_lib = runtime_root / "lib"
    runtime_bin.mkdir(parents=True)
    runtime_lib.mkdir()
    interpreter = runtime_bin / "python3"
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python3"
    venv_python.symlink_to(interpreter)
    command = venv_python
    if via_env:
        command = tmp_path / "agent"
        command.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        command.chmod(0o755)

    readable = _command_read_paths((str(command),), {"PATH": str(venv_bin)})

    assert interpreter.resolve() in readable
    assert runtime_lib.resolve() in readable
    assert runtime_root.resolve() not in readable
    assert tmp_path.resolve() not in readable


def test_claude_code_can_initialize_inside_the_read_only_boundary(tmp_path: Path) -> None:
    """The real harness must survive Foundation startup inside Seatbelt."""

    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("Claude Code is not installed")
    with contained_frontier_process(
        (claude, "--version"),
        tmp_path,
        posture=ReadOnlyInspection(),
        harness=FrontierHarness.CLAUDE,
    ) as contained:
        result = _run(contained.command, tmp_path, dict(contained.environment))

    assert result.returncode == 0, result.stderr
    assert "Claude Code" in result.stdout


def test_frontier_process_can_run_a_child_through_a_pseudoterminal(tmp_path: Path) -> None:
    """Harness tool processes need a PTY without broader host-device access."""

    script = """
import os
import subprocess

master, slave = os.openpty()
result = subprocess.run(
    ("/usr/bin/true",),
    stdin=slave,
    stdout=slave,
    stderr=slave,
    check=False,
)
os.close(slave)
os.close(master)
raise SystemExit(result.returncode)
"""
    with contained_frontier_process(
        (sys.executable, "-c", script),
        tmp_path,
        posture=ReadOnlyInspection(),
        harness=FrontierHarness.CODEX,
    ) as contained:
        result = _run(contained.command, tmp_path, dict(contained.environment))

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "posture",
    (ReadOnlyInspection(), UnattendedImplementation()),
    ids=("read-only", "implementation"),
)
def test_claude_code_can_read_its_authenticated_session_inside_the_boundary(
    tmp_path: Path,
    posture: ReadOnlyInspection | UnattendedImplementation,
) -> None:
    """Both production postures must reach Claude's login keychain item."""

    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("Claude Code is not installed")
    host_result = _run((claude, "auth", "status", "--json"), tmp_path, dict(os.environ))
    host_payload = json.loads(host_result.stdout) if host_result.stdout.strip() else {}
    if not host_payload.get("loggedIn"):
        pytest.skip("Claude Code host session is not authenticated")

    with contained_frontier_process(
        (claude, "auth", "status", "--json"),
        tmp_path,
        posture=posture,
        harness=FrontierHarness.CLAUDE,
    ) as contained:
        result = _run(contained.command, tmp_path, dict(contained.environment))

    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    assert result.returncode == 0, result.stderr
    assert payload.get("loggedIn") is True


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
    require_uncontained_scope(
        reason="operator-token fixture and policy owner must share the same operator identity",
        required_flag="AIDASHOS_REQUIRE_HOST_CONTAINMENT_TESTS",
    )
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
    require_uncontained_scope(
        reason="reader/writer endpoint isolation needs host-owned listening sockets",
        required_flag="LOCAL_AGENT_REQUIRE_HOST_NETWORK_TESTS",
    )
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
        # This oracle uses only stdlib sockets. Its executable and base library
        # are the declared runtime; it must not load the test runner's venv site.
        python = str(Path(sys.executable).resolve(strict=True))

        def connect(port: int) -> tuple[str, ...]:
            return (
                python,
                "-I",
                "-S",
                "-c",
                "import json, socket\n"
                "try:\n"
                f"    socket.create_connection(('127.0.0.1', {port}), 1).close()\n"
                "except OSError as error:\n"
                "    print(json.dumps({'connected': False, 'errno': error.errno}))\n"
                "    raise SystemExit(1)\n"
                "print(json.dumps({'connected': True}))\n",
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
    assert reader_result.returncode == 0, reader_result.stderr
    assert json.loads(reader_result.stdout) == {"connected": True}
    assert writer_result.returncode == 1, writer_result.stderr
    refused = json.loads(writer_result.stdout)
    assert refused["connected"] is False
    assert refused["errno"] in (errno.EPERM, errno.EACCES)
