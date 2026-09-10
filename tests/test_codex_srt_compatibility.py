# SPDX-License-Identifier: AGPL-3.0-or-later
"""No-model proof through the installed Codex command tool and pinned SRT.

Explicitly opt in with LOCAL_AGENT_SRT_PROBE_ROOT and LOCAL_AGENT_SRT_PROBE_NODE.
No dependency acquisition, authenticated session, or production ledger is used.
This proves offline tool compatibility, not authenticated review readiness.
"""

from __future__ import annotations

import json
import os
import platform
import selectors
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from contextlib import suppress
from pathlib import Path

import pytest

from local_first_agent_os.sandbox_runtime import ReadOnlyToolWorker, SandboxRuntimeInstallation


def _reply(process: subprocess.Popen[str], request_id: int) -> dict:
    assert process.stdout is not None
    deadline = time.monotonic() + 20
    line = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            assert selector.select(deadline - time.monotonic()), "Codex RPC timed out"
            # Never mix select() with a text reader's hidden read-ahead buffer.
            # Read through one newline so the next RPC owns all remaining bytes.
            char = os.read(process.stdout.fileno(), 1)
            assert char, "Codex RPC closed before replying"
            if char != b"\n":
                line.extend(char)
                continue
            message = json.loads(line)
            line.clear()
            if message.get("id") == request_id:
                assert "error" not in message, message
                return message["result"]
    raise AssertionError("Codex RPC timed out")


def test_single_boundary_codex_tool_controls(tmp_path: Path) -> None:
    root = os.environ.get("LOCAL_AGENT_SRT_PROBE_ROOT")
    node = os.environ.get("LOCAL_AGENT_SRT_PROBE_NODE")
    if platform.system() != "Darwin" or not root or not node:
        pytest.skip("explicit macOS SRT compatibility fixture not configured")
    installation = SandboxRuntimeInstallation.inspect(Path(root), Path(node))
    codex = shutil.which("codex")
    assert codex is not None
    repo = tmp_path / "repository"
    repo.mkdir()
    expected = "aidashos-offline-tool-canary\n"
    (repo / "canary.txt").write_text(expected)
    foreign = tmp_path / "foreign-secret.txt"
    foreign.write_text("never expose this fixture\n")
    worker = ReadOnlyToolWorker(installation, repo)
    command = [codex, "-c", "analytics.enabled=false", "app-server"]
    with (
        worker.contain_service(command) as contained,
        (tmp_path / "stderr.log").open("w+") as stderr,
    ):
        process = subprocess.Popen(
            contained.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=contained.environment,
        )
        assert process.stdin is not None
        stdin = process.stdin

        def send(payload: dict) -> None:
            stdin.write(json.dumps(payload) + "\n")
            stdin.flush()

        try:
            send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "aidashos_compatibility", "version": "1"},
                    },
                }
            )
            _reply(process, 1)
            send({"method": "initialized", "params": {}})

            def execute(number: int, command: list[str]) -> dict:
                send(
                    {
                        "id": number,
                        "method": "command/exec",
                        "params": {
                            "command": command,
                            "cwd": str(repo),
                            "timeoutMs": 5000,
                            "sandboxPolicy": {
                                "type": "externalSandbox",
                                "networkAccess": "restricted",
                            },
                        },
                    }
                )
                return _reply(process, number)

            read = execute(2, ["/bin/cat", "canary.txt"])
            assert read["exitCode"] == 0, read
            assert read["stdout"] == expected, read
            scratch = execute(7, ["/bin/sh", "-c", 'printf "%s" "$TMPDIR"'])
            assert scratch["exitCode"] == 0, scratch
            assert Path(scratch["stdout"]).resolve() == contained.scratch_path
            private_write = execute(
                8,
                ["/bin/sh", "-c", 'printf private > "$TMPDIR/canary"; cat "$TMPDIR/canary"'],
            )
            assert private_write["exitCode"] == 0, private_write
            assert private_write["stdout"] == "private", private_write
            shared_root = Path("/private/tmp/claude")
            created_shared_root = not shared_root.exists()
            shared_root.mkdir(exist_ok=True)
            try:
                with tempfile.TemporaryDirectory(prefix="aidashos-denied-", dir=shared_root) as raw:
                    shared_canary = Path(raw) / "canary"
                    shared_canary.write_text("unchanged")
                    shared_write = execute(
                        9,
                        ["/bin/sh", "-c", 'printf changed > "$1"', "probe", str(shared_canary)],
                    )
                    assert shared_write["exitCode"] != 0, shared_write
                    assert "permitted" in shared_write["stderr"].lower(), shared_write
                    assert shared_canary.read_text() == "unchanged"
            finally:
                if created_shared_root:
                    with suppress(OSError):
                        shared_root.rmdir()
            nested = execute(
                6,
                [
                    codex,
                    "sandbox",
                    "-c",
                    'sandbox_mode="read-only"',
                    "--",
                    "/bin/cat",
                    "canary.txt",
                ],
            )
            assert nested["exitCode"] == 71, nested
            assert "sandbox_apply" in nested["stderr"], nested
            write = execute(3, ["/bin/sh", "-c", "printf changed > canary.txt"])
            assert write["exitCode"] != 0, write
            assert "permitted" in write["stderr"].lower(), write
            denied = execute(4, ["/bin/cat", str(foreign)])
            assert denied["exitCode"] != 0, denied
            assert "permitted" in denied["stderr"].lower(), denied
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = listener.getsockname()[1]
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    connection, _ = listener.accept()
                    connection.close()
                network = execute(
                    5,
                    [
                        "/usr/bin/curl",
                        "--noproxy",
                        "*",
                        "--connect-timeout",
                        "2",
                        "--max-time",
                        "3",
                        f"http://127.0.0.1:{port}",
                    ],
                )
                assert network["exitCode"] == 7, network
                listener.settimeout(0.1)
                with pytest.raises(TimeoutError):
                    listener.accept()
            assert (repo / "canary.txt").read_text() == expected
        except BaseException:
            stderr.flush()
            stderr.seek(0)
            print(stderr.read()[-4000:])
            raise
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
